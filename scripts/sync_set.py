"""Cache one Magic set's cards from Scryfall into the `cards` table.

Usage:
    python scripts/sync_set.py mkm
"""

import argparse
import asyncio
import sys
from pathlib import Path

import aiohttp

sys.path.append(str(Path(__file__).resolve().parent.parent))

from utils import database  # noqa: E402

SCRYFALL_SEARCH_URL = "https://api.scryfall.com/cards/search"

# Non-playable-pack layouts to skip: tokens, art cards, etc.
SKIP_LAYOUTS = {"token", "art_series", "double_faced_token", "emblem", "planar", "scheme", "vanguard"}


def _image_url(card: dict) -> str | None:
    if "image_uris" in card:
        return card["image_uris"].get("normal")
    faces = card.get("card_faces") or []
    if faces and "image_uris" in faces[0]:
        return faces[0]["image_uris"].get("normal")
    return None


async def fetch_set(session: aiohttp.ClientSession, set_code: str) -> list[dict]:
    cards: list[dict] = []
    url = SCRYFALL_SEARCH_URL
    params: dict | None = {"q": f"set:{set_code}", "unique": "prints"}

    while url:
        async with session.get(url, params=params) as resp:
            if resp.status == 404:
                return []
            resp.raise_for_status()
            payload = await resp.json()

        cards.extend(payload.get("data", []))
        url = payload.get("next_page")
        params = None  # next_page is already a full URL with query params baked in
        if url:
            await asyncio.sleep(0.1)  # stay well under Scryfall's rate-limit guidance

    return cards


def _to_row(card: dict) -> tuple:
    return (
        card["id"],
        card["oracle_id"],
        card["name"],
        card["set"],
        card["collector_number"],
        card["rarity"],
        "Basic Land" in card.get("type_line", ""),
        card.get("frame_effects") or [],
        card.get("finishes") or [],
        bool(card.get("full_art", False)),
        card.get("border_color"),
        card.get("mana_cost"),
        card.get("type_line"),
        _image_url(card),
    )


async def sync_set(set_code: str) -> int:
    set_code = set_code.lower()
    async with aiohttp.ClientSession(headers={"User-Agent": "UrzaDiscordBot/1.0"}) as session:
        cards = await fetch_set(session, set_code)

    rows = [
        _to_row(card)
        for card in cards
        if not card.get("digital") and card.get("layout") not in SKIP_LAYOUTS
    ]

    await database.init_db()
    try:
        return await database.upsert_cards(rows)
    finally:
        await database.close_db()


def main() -> None:
    parser = argparse.ArgumentParser(description="Cache a Magic set's cards from Scryfall.")
    parser.add_argument("set_code", help="Set code, e.g. mkm, woe, ltr")
    args = parser.parse_args()

    count = asyncio.run(sync_set(args.set_code))
    if count == 0:
        print(f"No cards found for set '{args.set_code.lower()}' — check the set code.")
    else:
        print(f"Cached {count} cards for set '{args.set_code.lower()}'.")


if __name__ == "__main__":
    main()
