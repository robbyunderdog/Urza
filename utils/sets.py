"""Free-text set-name resolution: turns whatever a user types into `set:`
(a code like "mh3", a full name like "Modern Horizons 3", a typo, or a
partial name) into a single row from the `sets` table.

The whole `sets` table is small (a couple hundred rows) and changes rarely,
so it's cached in memory (see `_get_cached_sets`) rather than queried fresh
on every command invocation — nearly every slash command in this bot takes
a `set` argument, so this cache turns what would be 1-3 DB round trips per
command into zero for the common case.
"""

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
    """Return every cached `sets` row, refetching from the database only if
    the in-memory cache is empty or older than `_CACHE_TTL_SECONDS`."""
    global _cache, _cache_loaded_at
    now = time.monotonic()
    if _cache is None or (now - _cache_loaded_at) > _CACHE_TTL_SECONDS:
        _cache = await database.get_all_sets()
        _cache_loaded_at = now
    return _cache


class SetNotFoundError(Exception):
    """Raised when no set matches the query at all — not even a fuzzy one."""

    pass


class AmbiguousSetError(Exception):
    """Raised when more than one set could plausibly match the query
    (e.g. multiple sets whose name contains the search text). Carries the
    matching candidates so the caller can show a "did you mean" prompt —
    the exception's own message already renders that prompt as text."""

    def __init__(self, candidates: list[asyncpg.Record]):
        self.candidates = candidates
        shown = candidates[:MAX_CANDIDATES_SHOWN]
        names = ", ".join(f"{c['name']} ({c['code']})" for c in shown)
        super().__init__(f"Multiple sets match — did you mean: {names}?")


async def resolve_set(query: str, *, exclude_commander: bool = False) -> asyncpg.Record:
    """Resolve free text like 'mh3' or 'Modern Horizons 3' to a sets row.

    Tries, in order: exact code, exact name, unique substring match, then a
    fuzzy match. Raises SetNotFoundError or AmbiguousSetError otherwise.
    Reads from an in-memory cache of the (small, rarely-changing) sets
    table rather than hitting the database on every call.

    exclude_commander drops set_type == 'commander' rows from the
    substring/fuzzy matching pool (but not the exact code/name checks) —
    used by /open, where a Commander deck's name (e.g. "Tarkir: Dragonstorm
    Commander") otherwise fuzzy-matches right alongside its base set and
    turns an unambiguous query into a bogus "did you mean" prompt, even
    though the Commander deck was never openable on its own anyway.
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

    candidates = [s for s in all_sets if s["set_type"] != "commander"] if exclude_commander else all_sets

    substring_matches = [s for s in candidates if lowered in s["name"].lower()]
    if len(substring_matches) == 1:
        return substring_matches[0]
    if len(substring_matches) > 1:
        raise AmbiguousSetError(substring_matches)

    close_names = difflib.get_close_matches(
        lowered, [s["name"].lower() for s in candidates], n=MAX_CANDIDATES_SHOWN, cutoff=0.6
    )
    if close_names:
        matched = [s for s in candidates if s["name"].lower() in close_names]
        if len(matched) == 1:
            return matched[0]
        raise AmbiguousSetError(matched)

    raise SetNotFoundError(f"No set found matching '{query}'.")
