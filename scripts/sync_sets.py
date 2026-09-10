"""Cache Magic set metadata from Scryfall, used to resolve free-text set
names ("mh3", "Modern Horizons 3") to a set code. Cheap and fast — run this
any time new sets are announced.

Only keeps "major" sets (see utils.sets.MAJOR_SET_TYPES) — core sets,
expansions, masters sets, draft-innovation products (Modern Horizons,
Battlebond, etc.), and Un-sets — plus any set-specific Commander deck whose
parent_set_code points at one of those (e.g. "Dominaria United Commander"
because of "Dominaria United"). Skips promos, tokens, generic yearly
Commander releases, memorabilia, and other supplemental products that
aren't sets people actually collect.

Usage:
    python scripts/sync_sets.py
"""

import asyncio
import sys
from datetime import date
from pathlib import Path

import aiohttp

sys.path.append(str(Path(__file__).resolve().parent.parent))

from utils import database  # noqa: E402
from utils.sets import MAJOR_SET_TYPES  # noqa: E402

SETS_URL = "https://api.scryfall.com/sets"


def _parse_date(value: str | None) -> date | None:
    """Parse Scryfall's ISO date string into a `date`, or None if the set
    has no release date on file (e.g. some unreleased/undated products)."""
    if not value:
        return None
    return date.fromisoformat(value)


async def fetch_sets(session: aiohttp.ClientSession) -> list[dict]:
    """Fetch every set from Scryfall's paginated /sets endpoint, following
    `next_page` links until exhausted. A small sleep between pages keeps
    well under Scryfall's rate-limit guidance."""
    sets: list[dict] = []
    url = SETS_URL
    while url:
        async with session.get(url) as resp:
            resp.raise_for_status()
            payload = await resp.json()
        sets.extend(payload.get("data", []))
        url = payload.get("next_page")
        if url:
            await asyncio.sleep(0.1)
    return sets


async def sync_sets() -> tuple[int, int]:
    """Fetch all sets from Scryfall, filter down to the curated "major set"
    list (see utils.sets.MAJOR_SET_TYPES) plus any Commander deck
    bonus-linked to one of those, and upsert them into the `sets` table.
    Returns (sets upserted, of those how many were bonus-linked Commander decks).
    """
    async with aiohttp.ClientSession(headers={"User-Agent": "UrzaDiscordBot/1.0"}) as session:
        raw_sets = await fetch_sets(session)

    major_codes = {s["code"] for s in raw_sets if s.get("set_type") in MAJOR_SET_TYPES}
    bonus_codes = {
        s["code"]
        for s in raw_sets
        if s.get("set_type") == "commander" and s.get("parent_set_code") in major_codes
    }
    allowed_codes = major_codes | bonus_codes

    rows = [
        (
            s["code"],
            s["name"],
            s.get("set_type"),
            _parse_date(s.get("released_at")),
            s.get("parent_set_code"),
        )
        for s in raw_sets
        if s["code"] in allowed_codes
    ]

    await database.init_db()
    try:
        count = await database.upsert_sets(rows)
        return count, len(bonus_codes)
    finally:
        await database.close_db()


def main() -> None:
    count, bonus_count = asyncio.run(sync_sets())
    print(f"Cached {count} sets ({bonus_count} of them Commander decks bonus-linked to a base set).")


if __name__ == "__main__":
    main()
