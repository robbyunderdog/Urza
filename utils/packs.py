"""Booster-pack simulation: decides what cards come out of a Play or
Collector booster and records them into the opener's collection. This is
the "game rules" module — no Discord code lives here, so the odds can be
reasoned about (or unit tested) independently of the /open command in
cogs/packs.py, which only handles cooldowns and presenting the result.

Both booster types build a pack the same general way:
1. Roll a rarity for each of the 14 main slots, independently, from a
   weighted distribution (PLAY_RARITY_WEIGHTS / COLLECTOR_RARITY_WEIGHTS).
2. Fetch one random base (non-special-treatment) printing per distinct
   (set, rarity) group actually needed — one query per group, not per card.
3. For a chosen few slots, maybe upgrade the drawn card to a
   special-treatment printing (extended art/showcase/borderless) of the
   same card, and/or mark it foil.
4. Add exactly one basic land as the 15th card.
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
# 14 independently rolled by rarity, + 1 basic land. Of the 14: 2 (picked at
# random) are guaranteed foil if that printing supports foil, and a
# different 2 each get a chance to be upgraded to a special-treatment
# printing of that same card, if one exists in the set.
PLAY_RARITY_WEIGHTS = {"mythic": 1, "rare": 10, "uncommon": 20, "common": 69}
PLAY_CARD_COUNT = 14
PLAY_GUARANTEED_FOILS = 2
PLAY_SPECIAL_ELIGIBLE = 2
PLAY_SPECIAL_CHANCE = 0.20
PLAY_LAND_FOIL_CHANCE = 0.20

# --- Collector Booster: 15 cards --------------------------------------------
# 14 independently rolled by rarity (richer distribution than Play), + 1
# land (foil, prefers full-art). Every one of the 14 is foil whenever the
# printing supports it. 5 of the 14 each get a chance at a special-treatment
# upgrade. Each of the 14 also independently has a chance to be swapped for
# a card from the set's bonus-linked Commander deck instead.
COLLECTOR_RARITY_WEIGHTS = {"mythic": 10, "rare": 20, "uncommon": 30, "common": 40}
COLLECTOR_CARD_COUNT = 14
COLLECTOR_SPECIAL_ELIGIBLE = 5
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
    foil: bool = False
    special: bool = False  # extended art / showcase treatment
    bonus: bool = False  # from a bonus-linked Commander deck, not the base set


def _weighted_rarity(weights: dict[str, int]) -> str:
    """Pick one rarity at random, weighted by the given {rarity: weight}
    mapping (e.g. PLAY_RARITY_WEIGHTS). Weights don't need to sum to 100 —
    random.choices normalizes them internally."""
    return random.choices(list(weights.keys()), weights=list(weights.values()))[0]


def _can_be_foil(finishes) -> bool:
    """Whether this printing has a foil version at all — Scryfall's
    `finishes` list on the printing (e.g. ["nonfoil", "foil"]). Some
    printings (certain promos, oversized cards) are foil-only or
    nonfoil-only, so this must be checked before ever marking a pull foil."""
    return "foil" in (finishes or [])


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
    foil_chance: float = 0.0,
    special_chance: float = 0.0,
    bonus: bool = False,
) -> PulledCard:
    """Given an already-drawn base printing, maybe upgrade it to a
    special-treatment printing of the same card, then decide foil based on
    whatever printing it ends up as."""
    card_id, oracle_id, name, drawn_rarity, image_url, finishes = (
        row["id"],
        row["oracle_id"],
        row["name"],
        row["rarity"],
        row["image_url"],
        row["finishes"],
    )
    special = False

    if special_chance > 0 and random.random() < special_chance:
        alt = await database.get_special_treatment_printing(oracle_id, set_code)
        if alt:
            card_id, name, drawn_rarity, image_url, finishes = (
                alt["id"],
                alt["name"],
                alt["rarity"],
                alt["image_url"],
                alt["finishes"],
            )
            special = True

    foil = _can_be_foil(finishes) and random.random() < foil_chance
    return PulledCard(card_id, name, drawn_rarity, image_url, foil, special, bonus)


async def _draw_land(set_code: str, *, foil_chance: float) -> PulledCard | None:
    """Draw the pack's single basic-land slot. Returns None if the set has
    no basic lands cached at all (some sets/products genuinely don't ship
    one) — the caller simply omits the land rather than erroring."""
    rows = await database.get_random_basic_lands(set_code, 1)
    if not rows:
        return None
    row = rows[0]
    foil = _can_be_foil(row["finishes"]) and random.random() < foil_chance
    return PulledCard(row["id"], row["name"], row["rarity"], row["image_url"], foil)


async def _open_play_booster(set_code: str) -> list[PulledCard]:
    """Build a 15-card Play Booster: 14 independently-rolled cards plus one
    basic land. 2 of the 14 slots (picked at random) are guaranteed foil; a
    different 2 each get a chance at a special-treatment upgrade."""
    rarities = [_weighted_rarity(PLAY_RARITY_WEIGHTS) for _ in range(PLAY_CARD_COUNT)]
    rows_by_group = await _fetch_base_cards_by_group(Counter((set_code, r) for r in rarities))
    group_pointers: dict[tuple, int] = {}

    foil_indices = set(random.sample(range(PLAY_CARD_COUNT), PLAY_GUARANTEED_FOILS))
    special_indices = set(random.sample(range(PLAY_CARD_COUNT), PLAY_SPECIAL_ELIGIBLE))

    pulled: list[PulledCard] = []
    for i, rarity in enumerate(rarities):
        group = (set_code, rarity)
        pointer = group_pointers.get(group, 0)
        rows = rows_by_group[group]
        if pointer >= len(rows):
            continue  # not enough cards of that rarity cached — skip this slot
        group_pointers[group] = pointer + 1

        card = await _finish_card(
            rows[pointer],
            set_code,
            foil_chance=1.0 if i in foil_indices else 0.0,
            special_chance=PLAY_SPECIAL_CHANCE if i in special_indices else 0.0,
        )
        pulled.append(card)

    land = await _draw_land(set_code, foil_chance=PLAY_LAND_FOIL_CHANCE)
    if land:
        pulled.append(land)

    return pulled


async def _open_collector_booster(set_code: str, bonus_set_code: str | None) -> list[PulledCard]:
    """Build a 15-card Collector Booster: 14 cards from a richer rarity
    distribution than Play (every one of which is foil whenever possible),
    plus a guaranteed-foil land. 5 of the 14 slots each get a (higher)
    chance at a special-treatment upgrade, and — if this set has a
    bonus-linked Commander deck — each of the 14 slots independently has a
    chance to be swapped for a card from that deck instead of the base set.
    """
    plans = []  # (rarity, source_set, is_bonus) per slot
    for _ in range(COLLECTOR_CARD_COUNT):
        rarity = _weighted_rarity(COLLECTOR_RARITY_WEIGHTS)
        use_bonus = bool(bonus_set_code) and random.random() < COLLECTOR_BONUS_CHANCE
        source_set = bonus_set_code if use_bonus else set_code
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
            continue
        group_pointers[group] = pointer + 1

        card = await _finish_card(
            rows[pointer],
            source_set,
            foil_chance=1.0,
            special_chance=COLLECTOR_SPECIAL_CHANCE if i in special_indices else 0.0,
            bonus=is_bonus,
        )
        pulled.append(card)

    land = await _draw_land(set_code, foil_chance=1.0)
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
