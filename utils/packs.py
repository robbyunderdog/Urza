"""Booster-pack simulation: decides what cards come out of a pack and
records them into the opener's collection. This is the "game rules" module
— no Discord code lives here, so the odds can be reasoned about (or unit
tested) independently of the /rip command in cogs/packs.py, which only
handles cooldowns and presenting the result.

There is one pack type. Its pool is every non-basic-land printing in the
set — alternate-art printings included — and every printing of a given
rarity has an equal chance of being drawn (alternate art is flagged for
display, but isn't weighted any differently). Every set, set-specific
Commander decks included, is opened (and completed) on its own; there's no
cross-set bonus pool. A pack is built like this:
1. Roll a rarity for each of the 14 main slots, independently, from
   PACK_RARITY_WEIGHTS — filtered down to rarities the pool actually has
   any cards of, so a set missing a whole rarity (e.g. no mythics in sets
   printed before 2008) never rolls a slot it can't fill.
2. Fetch random printings for each distinct rarity actually needed — one
   query per rarity, not per card.
3. Add exactly one basic land as the 15th card (preferring full-art), if
   the set has any basic lands cached at all — some small/old sets never
   printed their own (e.g. Prophecy), in which case the pack is 14 cards.

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

RARITY_ORDER = {"mythic": 0, "rare": 1, "uncommon": 2, "common": 3}

# Key under which the pack cooldown is stored in pack_cooldowns.booster_type.
PACK_TYPE = "pack"

# 14 independently rolled by rarity, + 1 basic land.
PACK_RARITY_WEIGHTS = {"mythic": 5, "rare": 15, "uncommon": 35, "common": 45}
PACK_CARD_COUNT = 14


@dataclass
class PulledCard:
    """One card pulled out of a booster — everything the presentation layer
    (cogs/packs.py) needs to render it."""

    card_id: int
    name: str
    rarity: str
    image_url: str | None
    special: bool = False  # extended art / showcase / borderless / full art


def _weighted_rarity(weights: dict[str, int]) -> str:
    """Pick one rarity at random, weighted by the given {rarity: weight}
    mapping. Weights don't need to sum to 100 — random.choices normalizes
    them internally."""
    return random.choices(list(weights.keys()), weights=list(weights.values()))[0]


def _filtered_weights(weights: dict[str, int], available: set[str]) -> dict[str, int]:
    """Restrict a rarity-weight table to rarities the pool actually has
    cards for (see database.get_available_rarities), so _weighted_rarity can
    never roll a rarity that would just come back empty and silently shrink
    the pack by a slot. Falls back to the unfiltered weights in the
    (should-be-unreachable, since set_is_cached is already checked before a
    pack is opened) case where the pool is empty — the empty-group skip in
    _build_pack is the backstop."""
    filtered = {rarity: weight for rarity, weight in weights.items() if rarity in available}
    return filtered or weights


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
    return PulledCard(row["id"], row["name"], row["rarity"], row["image_url"], row["is_special"])


async def _build_pack(set_code: str) -> list[PulledCard]:
    """Build a 15-card pack: 14 independently-rolled cards from every
    printing in the set, plus one basic land (omitted if the set has none
    cached)."""
    available = await database.get_available_rarities(set_code)
    weights = _filtered_weights(PACK_RARITY_WEIGHTS, available)
    rarities = [_weighted_rarity(weights) for _ in range(PACK_CARD_COUNT)]

    rows_by_rarity = {
        rarity: await database.get_random_pack_cards(set_code, rarity, count)
        for rarity, count in Counter(rarities).items()
    }
    pointers: dict[str, int] = {}

    pulled: list[PulledCard] = []
    for rarity in rarities:
        pointer = pointers.get(rarity, 0)
        rows = rows_by_rarity[rarity]
        if pointer >= len(rows):
            continue  # not enough distinct cards of that rarity cached — skip this slot
        pointers[rarity] = pointer + 1
        row = rows[pointer]
        pulled.append(PulledCard(row["id"], row["name"], row["rarity"], row["image_url"], row["is_special"]))

    land = await _draw_land(set_code)
    if land:
        pulled.append(land)

    return pulled


async def generate_pack(set_code: str) -> list[PulledCard]:
    """Generate one pack of `set_code` without recording it anywhere — used
    by open_pack; handy on its own for showing a pack without giving the
    cards to anyone."""
    return await _build_pack(set_code)


def sort_by_rarity(cards: list[PulledCard]) -> list[PulledCard]:
    """Rarest first, keeping pack order within a rarity (so the basic land,
    always drawn last, stays at the end of the commons)."""
    return sorted(cards, key=lambda card: RARITY_ORDER.get(card.rarity, len(RARITY_ORDER)))


async def open_pack(guild_id: int, user_id: int, set_code: str) -> list[PulledCard]:
    """Open one pack of `set_code`, add every pulled card to the user's
    per-server collection, and return the pulls (in pack order — the caller
    doesn't need to re-sort them for display).

    This is the only function in this module that touches the database for
    anything other than reading — the actual collection write happens here,
    once, after the whole pack has been generated.
    """
    pulled = await generate_pack(set_code)
    await database.add_to_collection(guild_id, user_id, [card.card_id for card in pulled])
    return pulled
