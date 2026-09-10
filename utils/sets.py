import difflib
import time

import asyncpg

from utils import database

MAX_CANDIDATES_SHOWN = 8

# The set of Scryfall set_types that get their own openable booster — matches
# draftsim.com/mtg-sets/'s curated list closely (core+expansion alone is
# ~141 sets vs. draftsim's stated ~150; adding masters/draft_innovation/funny
# brings it to ~198 vs. their ~200+ full table). Deliberately excludes:
# promo, memorabilia, duel_deck, token, box, masterpiece, starter,
# from_the_vault, eternal (Arena-only), arsenal, planechase, premium_deck,
# spellbook, archenemy.
#
# set_type == "commander" is a special case, not in this set: a set-specific
# Commander deck (e.g. "Dominaria United Commander") is kept and cached if
# it has a parent_set_code pointing at a set in here, but never gets its own
# booster — its cards are only reachable as a bonus pull from the base set's
# Collector Boosters (see BONUS_SLOT_CHANCE in utils/packs.py). The generic
# yearly "Commander 20XX" releases have no parent_set_code and are excluded
# entirely, same as before.
MAJOR_SET_TYPES = {"core", "expansion", "masters", "draft_innovation", "funny"}

# The sets table is small (a couple hundred rows) and changes only when an
# admin runs a sync script, but it's read on nearly every command (anything
# that takes a `set` argument). Caching it in memory turns that into zero
# extra DB round trips for the common case, instead of 1-3 per command.
_CACHE_TTL_SECONDS = 600
_cache: list[asyncpg.Record] | None = None
_cache_loaded_at: float = 0.0


def invalidate_cache() -> None:
    """Force the next resolve_set call to refetch from the database."""
    global _cache
    _cache = None


async def _get_cached_sets() -> list[asyncpg.Record]:
    global _cache, _cache_loaded_at
    now = time.monotonic()
    if _cache is None or (now - _cache_loaded_at) > _CACHE_TTL_SECONDS:
        _cache = await database.get_all_sets()
        _cache_loaded_at = now
    return _cache


class SetNotFoundError(Exception):
    pass


class AmbiguousSetError(Exception):
    def __init__(self, candidates: list[asyncpg.Record]):
        self.candidates = candidates
        shown = candidates[:MAX_CANDIDATES_SHOWN]
        names = ", ".join(f"{c['name']} ({c['code']})" for c in shown)
        super().__init__(f"Multiple sets match — did you mean: {names}?")


async def resolve_set(query: str) -> asyncpg.Record:
    """Resolve free text like 'mh3' or 'Modern Horizons 3' to a sets row.

    Tries, in order: exact code, exact name, unique substring match, then a
    fuzzy match. Raises SetNotFoundError or AmbiguousSetError otherwise.
    Reads from an in-memory cache of the (small, rarely-changing) sets
    table rather than hitting the database on every call.
    """
    query = query.strip()
    if not query:
        raise SetNotFoundError("No set specified.")

    all_sets = await _get_cached_sets()
    if not all_sets:
        raise SetNotFoundError(
            "No set data cached yet — an admin needs to run `python scripts/sync_sets.py`."
        )

    lowered = query.lower()

    for s in all_sets:
        if s["code"].lower() == lowered:
            return s
    for s in all_sets:
        if s["name"].lower() == lowered:
            return s

    substring_matches = [s for s in all_sets if lowered in s["name"].lower()]
    if len(substring_matches) == 1:
        return substring_matches[0]
    if len(substring_matches) > 1:
        raise AmbiguousSetError(substring_matches)

    close_names = difflib.get_close_matches(
        lowered, [s["name"].lower() for s in all_sets], n=MAX_CANDIDATES_SHOWN, cutoff=0.6
    )
    if close_names:
        matched = [s for s in all_sets if s["name"].lower() in close_names]
        if len(matched) == 1:
            return matched[0]
        raise AmbiguousSetError(matched)

    raise SetNotFoundError(f"No set found matching '{query}'.")
