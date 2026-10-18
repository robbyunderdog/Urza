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
2. Fetch random printings for every slot, plus the land, in a single
   query (see database.get_pack_rows).
3. Include exactly one basic land as the 15th card (preferring full-art), if
   the set has any basic lands cached at all — some small/old sets never
   printed their own (e.g. Prophecy), in which case the pack is 14 cards.

No foils: every pull is presentation-plain — this bot doesn't model foil
odds.
"""

import random
import time
from collections import Counter, defaultdict
from dataclasses import dataclass

from utils import database

RARITY_EMOJI = {
    "common": "⚪",
    "uncommon": "🔵",
    "rare": "🟡",
    "mythic": "🟠",
}

RARITY_ORDER = {"mythic": 0, "rare": 1, "uncommon": 2, "common": 3}

# Shown after the name of an alternate-art printing (PulledCard.special).
SPECIAL_EMOJI = "🎨"

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
    set_code: str | None = None  # used to file the image cache by set (utils/pack_image.py)


def card_line(card: PulledCard) -> str:
    """Rarity emoji + name, plus SPECIAL_EMOJI for an alternate-art printing."""
    line = f"{RARITY_EMOJI.get(card.rarity, '')} {card.name}"
    return f"{line} {SPECIAL_EMOJI}" if card.special else line


def _weighted_rarity(weights: dict[str, int]) -> str:
    """Pick one rarity at random, weighted by the given {rarity: weight}
    mapping. Weights don't need to sum to 100 — random.choices normalizes
    them internally."""
    return random.choices(list(weights.keys()), weights=list(weights.values()))[0]


def _filtered_weights(weights: dict[str, int], available: set[str]) -> dict[str, int]:
    """Restrict a rarity-weight table to rarities the pool actually has
    cards for (see available_rarities), so _weighted_rarity can never roll a
    rarity that would just come back empty and silently shrink the pack by
    a slot. Falls back to the unfiltered weights in the
    (should-be-unreachable, since /rip checks available_rarities before
    opening) case where the pool is empty — the empty-bucket skip in
    generate_pack is the backstop."""
    filtered = {rarity: weight for rarity, weight in weights.items() if rarity in available}
    return filtered or weights


# Which rarities each set has, per set_code. Card data only changes when an
# admin runs a sync script, so this is cached in memory (same TTL idea as
# utils/sets.py) to save a database round trip on every /rip.
_RARITY_CACHE_TTL_SECONDS = 600
_rarity_cache: dict[str, tuple[float, set[str]]] = {}


async def available_rarities(set_code: str) -> set[str]:
    """Rarities with at least one non-basic printing cached for this set —
    empty if the set has no cards cached at all, so this doubles as the
    "is this set openable" check."""
    now = time.monotonic()
    cached = _rarity_cache.get(set_code)
    if cached is None or now - cached[0] > _RARITY_CACHE_TTL_SECONDS:
        cached = (now, await database.get_available_rarities(set_code))
        _rarity_cache[set_code] = cached
    return cached[1]


def _to_card(row, set_code: str) -> PulledCard:
    return PulledCard(row["id"], row["name"], row["rarity"], row["image_url"], row["is_special"], set_code)


async def generate_pack(set_code: str) -> list[PulledCard]:
    """Generate one 15-card pack of `set_code` without recording it
    anywhere: 14 independently-rolled cards from every printing in the set,
    plus one basic land (omitted if the set has none cached). One database
    round trip — see database.get_pack_rows."""
    weights = _filtered_weights(PACK_RARITY_WEIGHTS, await available_rarities(set_code))
    rarities = [_weighted_rarity(weights) for _ in range(PACK_CARD_COUNT)]
    rows = await database.get_pack_rows(set_code, dict(Counter(rarities)))

    buckets: dict[str, list] = defaultdict(list)
    land = None
    for row in rows:
        if row["is_land"]:
            land = row
        else:
            buckets[row["rarity"]].append(row)

    pulled: list[PulledCard] = []
    for rarity in rarities:
        if not buckets[rarity]:
            continue  # not enough distinct cards of that rarity cached — skip this slot
        pulled.append(_to_card(buckets[rarity].pop(), set_code))
    if land:
        pulled.append(_to_card(land, set_code))
    return pulled


def sort_by_rarity(cards: list[PulledCard]) -> list[PulledCard]:
    """Rarest first, keeping pack order within a rarity (so the basic land,
    always drawn last, stays at the end of the commons)."""
    return sorted(cards, key=lambda card: RARITY_ORDER.get(card.rarity, len(RARITY_ORDER)))


async def record_pack(guild_id: int, user_id: int, pulled: list[PulledCard]) -> None:
    """Add every card in an opened pack to the user's per-server
    collection. Kept separate from generate_pack so /rip can render the
    pack image at the same time as this write."""
    await database.add_to_collection(guild_id, user_id, [card.card_id for card in pulled])
