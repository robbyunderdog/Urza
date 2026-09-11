"""Booster-pack simulation: decides what cards come out of a Play or
Collector booster and records them into the opener's collection. This is
the "game rules" module — no Discord code lives here, so the odds can be
reasoned about (or unit tested) independently of the /ripbooster and
/ripcollector commands in cogs/packs.py, which only handle cooldowns and
presenting the result.

Both booster types build a pack the same general way:
1. Roll a rarity for each of the 14 main slots, independently, from a
   weighted distribution (PLAY_RARITY_WEIGHTS / COLLECTOR_RARITY_WEIGHTS) —
   filtered down to rarities the set actually has any cards of, so a set
   missing a whole rarity (e.g. no mythics in sets printed before 2008)
   never rolls a slot it can't fill.
2. Fetch one random base (non-special-treatment) printing per distinct
   (set, rarity) group actually needed — one query per group, not per card.
3. For a chosen few slots, maybe upgrade the drawn card to a
   special-treatment printing (extended art/showcase/borderless) of the
   same card.
4. Add exactly one basic land as the 15th card, if the set has any basic
   lands cached at all — some small/old sets never printed their own
   (e.g. Prophecy), in which case the pack is 14 cards instead of 15.

No foils: every pull is presentation-plain — this bot doesn't model foil
odds.
"""

import random
from collections import Counter
from dataclasses import dataclass

from utils import database

RARITY_EMOJI = {
    "common": "⚪",
    "uncommon": "🔵",
    "rare": "🟡",
    "mythic": "🟠",
}

BOOSTER_LABELS = {
    "play": "Play Booster",
    "collector": "Collector Booster",
}

# --- Play Booster: 15 cards -------------------------------------------------
# 14 independently rolled by rarity, + 1 basic land. 2 of the 14 each get a
# chance to be upgraded to a special-treatment printing of that same card,
# if one exists in the set.
PLAY_RARITY_WEIGHTS = {"mythic": 1, "rare": 10, "uncommon": 20, "common": 69}
PLAY_CARD_COUNT = 14
PLAY_SPECIAL_ELIGIBLE = 2
PLAY_SPECIAL_CHANCE = 0.20

# --- Collector Booster: 15 cards --------------------------------------------
# 14 independently rolled by rarity (richer distribution than Play), + 1
# land (prefers full-art, if this set has any). 5 of the 14 each get a
# chance at a special-treatment upgrade. Each of the 14 also independently
# has a chance to be swapped for a card from the set's bonus-linked
# Commander deck instead.
COLLECTOR_RARITY_WEIGHTS = {"mythic": 10, "rare": 20, "uncommon": 30, "common": 40}
COLLECTOR_CARD_COUNT = 14
COLLECTOR_SPECIAL_ELIGIBLE = 7
COLLECTOR_SPECIAL_CHANCE = 0.40
COLLECTOR_BONUS_CHANCE = 0.10


@dataclass
class PulledCard:
    """One card pulled out of a booster — everything the presentation layer
    (cogs/packs.py's PackRevealView) needs to render it, plus whatever
    cosmetic flags apply to this particular pull."""

    card_id: int
    name: str
    rarity: str
    image_url: str | None
    special: bool = False  # extended art / showcase treatment
    bonus: bool = False  # from a bonus-linked Commander deck, not the base set


def _weighted_rarity(weights: dict[str, int]) -> str:
    """Pick one rarity at random, weighted by the given {rarity: weight}
    mapping (e.g. PLAY_RARITY_WEIGHTS). Weights don't need to sum to 100 —
    random.choices normalizes them internally."""
    return random.choices(list(weights.keys()), weights=list(weights.values()))[0]


def _filtered_weights(weights: dict[str, int], available: set[str]) -> dict[str, int]:
    """Restrict a rarity-weight table to rarities this specific set/source
    actually has cards for (see database.get_available_rarities), so
    _weighted_rarity can never roll a rarity that would just come back
    empty and silently shrink the pack by a slot. Falls back to the
    unfiltered weights in the (should-be-unreachable, since set_is_cached
    is already checked before a pack is opened) case where the set has no
    qualifying cards in any rarity at all — the pre-existing empty-group
    skip in _open_play_booster/_open_collector_booster is the backstop."""
    filtered = {rarity: weight for rarity, weight in weights.items() if rarity in available}
    return filtered or weights


async def _fetch_base_cards_by_group(groups: Counter) -> dict[tuple, list]:
    """Fetch every (set_code, rarity) group's cards in one query per group,
    instead of one query per card — a 14-card pack only needs a handful of
    distinct (set, rarity) combinations, not 14 round trips. Each group's
    rows come back distinct (no duplicate printing within that group)."""
    rows_by_group: dict[tuple, list] = {}
    for (set_code, rarity), count in groups.items():
        rows_by_group[(set_code, rarity)] = await database.get_random_cards(
            set_code, rarity, count, exclude_basic_land=True, exclude_special_treatment=True
        )
    return rows_by_group


async def _finish_card(
    row,
    set_code: str,
    *,
    special_chance: float = 0.0,
    bonus: bool = False,
) -> PulledCard:
    """Given an already-drawn base printing, maybe upgrade it to a
    special-treatment printing of the same card."""
    card_id, oracle_id, name, drawn_rarity, image_url = (
        row["id"],
        row["oracle_id"],
        row["name"],
        row["rarity"],
        row["image_url"],
    )
    special = False

    if special_chance > 0 and random.random() < special_chance:
        alt = await database.get_special_treatment_printing(oracle_id, set_code)
        if alt:
            card_id, name, drawn_rarity, image_url = (
                alt["id"],
                alt["name"],
                alt["rarity"],
                alt["image_url"],
            )
            special = True

    return PulledCard(card_id, name, drawn_rarity, image_url, special, bonus)


async def _draw_land(set_code: str) -> PulledCard | None:
    """Draw the pack's single basic-land slot, preferring a full-art
    printing when this set has one cached. Returns None if the set has no
    basic lands cached at all — some sets/products genuinely don't ship
    one (e.g. Prophecy) — the caller simply omits the land rather than
    substituting one from an unrelated set."""
    rows = await database.get_random_basic_lands(set_code, 1)
    if not rows:
        return None
    row = rows[0]
    return PulledCard(row["id"], row["name"], row["rarity"], row["image_url"])


async def _open_play_booster(set_code: str) -> list[PulledCard]:
    """Build a 15-card Play Booster: 14 independently-rolled cards plus one
    basic land (omitted if the set has none cached). 2 of the 14 slots each
    get a chance at a special-treatment upgrade."""
    available = await database.get_available_rarities(set_code)
    weights = _filtered_weights(PLAY_RARITY_WEIGHTS, available)
    rarities = [_weighted_rarity(weights) for _ in range(PLAY_CARD_COUNT)]
    rows_by_group = await _fetch_base_cards_by_group(Counter((set_code, r) for r in rarities))
    group_pointers: dict[tuple, int] = {}

    special_indices = set(random.sample(range(PLAY_CARD_COUNT), PLAY_SPECIAL_ELIGIBLE))

    pulled: list[PulledCard] = []
    for i, rarity in enumerate(rarities):
        group = (set_code, rarity)
        pointer = group_pointers.get(group, 0)
        rows = rows_by_group[group]
        if pointer >= len(rows):
            continue  # not enough distinct cards of that rarity cached — skip this slot
        group_pointers[group] = pointer + 1

        card = await _finish_card(
            rows[pointer],
            set_code,
            special_chance=PLAY_SPECIAL_CHANCE if i in special_indices else 0.0,
        )
        pulled.append(card)

    land = await _draw_land(set_code)
    if land:
        pulled.append(land)

    return pulled


async def _open_collector_booster(set_code: str, bonus_set_code: str | None) -> list[PulledCard]:
    """Build a 15-card Collector Booster: 14 cards from a richer rarity
    distribution than Play, plus a land (omitted if the set has none
    cached). 5 of the 14 slots each get a (higher) chance at a
    special-treatment upgrade, and — if this set has a bonus-linked
    Commander deck — each of the 14 slots independently has a chance to be
    swapped for a card from that deck instead of the base set.
    """
    base_available = await database.get_available_rarities(set_code)
    bonus_available = await database.get_available_rarities(bonus_set_code) if bonus_set_code else set()

    plans = []  # (rarity, source_set, is_bonus) per slot
    for _ in range(COLLECTOR_CARD_COUNT):
        # Source is decided before rarity so the rarity roll can be filtered
        # to whichever specific set (base or bonus) will actually supply
        # this slot — the two sets can have different available rarities
        # (e.g. a Commander deck reliably has mythics; an old base set may not).
        use_bonus = bool(bonus_set_code) and bool(bonus_available) and random.random() < COLLECTOR_BONUS_CHANCE
        source_set = bonus_set_code if use_bonus else set_code
        available = bonus_available if use_bonus else base_available
        rarity = _weighted_rarity(_filtered_weights(COLLECTOR_RARITY_WEIGHTS, available))
        plans.append((rarity, source_set, use_bonus))

    rows_by_group = await _fetch_base_cards_by_group(Counter((source_set, rarity) for rarity, source_set, _ in plans))
    group_pointers: dict[tuple, int] = {}

    special_indices = set(random.sample(range(COLLECTOR_CARD_COUNT), COLLECTOR_SPECIAL_ELIGIBLE))

    pulled: list[PulledCard] = []
    for i, (rarity, source_set, is_bonus) in enumerate(plans):
        group = (source_set, rarity)
        pointer = group_pointers.get(group, 0)
        rows = rows_by_group[group]
        if pointer >= len(rows):
            continue  # not enough distinct cards of that rarity cached — skip this slot
        group_pointers[group] = pointer + 1

        card = await _finish_card(
            rows[pointer],
            source_set,
            special_chance=COLLECTOR_SPECIAL_CHANCE if i in special_indices else 0.0,
            bonus=is_bonus,
        )
        pulled.append(card)

    land = await _draw_land(set_code)
    if land:
        pulled.append(land)

    return pulled


async def open_pack(guild_id: int, user_id: int, set_code: str, booster_type: str = "play") -> list[PulledCard]:
    """Open one booster of the given type for `set_code`, add every pulled
    card to the user's per-server collection, and return the pulls (in pack
    order — the caller doesn't need to re-sort them for display).

    This is the only function in this module that touches the database for
    anything other than reading — the actual collection write happens here,
    once, after the whole pack has been generated.
    """
    if booster_type == "play":
        pulled = await _open_play_booster(set_code)
    elif booster_type == "collector":
        bonus_set_code = await database.get_bonus_set_code(set_code)
        pulled = await _open_collector_booster(set_code, bonus_set_code)
    else:
        raise ValueError(f"Unknown booster_type '{booster_type}'; expected 'play' or 'collector'")

    await database.add_to_collection(guild_id, user_id, [card.card_id for card in pulled])
    return pulled
