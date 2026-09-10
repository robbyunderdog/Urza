"""Cache every English Magic card printing from Scryfall's bulk data export.

This is the recommended one-time (then periodic) sync — after it, every set
is available to /open, /collection, etc. without syncing sets individually.
It downloads Scryfall's full "default_cards" bulk file (a gzip-compressed
JSONL export, tens of thousands of cards) and upserts everything in chunks,
so it takes a while; run it somewhere you can leave running (not inside a
Discord command).

Only keeps cards belonging to sets already present in the `sets` table, so
run scripts/sync_sets.py first — that script is what decides which sets are
"major" (see utils.sets.MAJOR_SET_TYPES) plus any bonus-linked Commander
decks.

Usage:
    python scripts/sync_all_cards.py
"""

import asyncio
import gzip
import json
import sys
from pathlib import Path

import aiohttp

sys.path.append(str(Path(__file__).resolve().parent.parent))

from utils import database  # noqa: E402

BULK_DATA_INFO_URL = "https://api.scryfall.com/bulk-data/default_cards"
CHUNK_SIZE = 1000

# Non-playable-pack layouts to skip: tokens, art cards, etc.
SKIP_LAYOUTS = {"token", "art_series", "double_faced_token", "emblem", "planar", "scheme", "vanguard"}


def _image_url(card: dict) -> str | None:
    if "image_uris" in card:
        return card["image_uris"].get("normal")
    faces = card.get("card_faces") or []
    if faces and "image_uris" in faces[0]:
        return faces[0]["image_uris"].get("normal")
    return None


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


def _wanted(card: dict, allowed_set_codes: set[str]) -> bool:
    return (
        card.get("lang") == "en"
        and not card.get("digital")
        and card.get("layout") not in SKIP_LAYOUTS
        and card.get("set") in allowed_set_codes
    )


def _safe_to_row(card: dict) -> tuple | None:
    """None for cards missing a field we need (e.g. some reversible_card
    layouts have oracle_id only per-face, not at the top level) — skipped
    rather than crashing the whole sync over a handful of oddball cards."""
    try:
        return _to_row(card)
    except KeyError:
        return None


async def fetch_bulk_meta(session: aiohttp.ClientSession) -> dict:
    async with session.get(BULK_DATA_INFO_URL) as resp:
        resp.raise_for_status()
        return await resp.json()


async def download_bytes(session: aiohttp.ClientSession, url: str) -> bytes:
    async with session.get(url) as resp:
        resp.raise_for_status()
        return await resp.read()


async def sync_all_cards() -> int:
    async with aiohttp.ClientSession(headers={"User-Agent": "UrzaDiscordBot/1.0"}) as session:
        meta = await fetch_bulk_meta(session)
        download_uri = meta.get("jsonl_download_uri") or meta.get("download_uri")
        size = meta.get("compressed_size") or meta.get("size") or 0
        print(f"Downloading '{meta['name']}' (~{size / 1_000_000:.0f} MB) from {download_uri} ...")
        raw = await download_bytes(session, download_uri)

    print(f"Downloaded {len(raw) / 1_000_000:.0f} MB, decompressing...")
    data = gzip.decompress(raw) if download_uri.endswith(".gz") else raw
    is_jsonl = ".jsonl" in download_uri

    await database.init_db()
    try:
        allowed_set_codes = {row["code"] for row in await database.get_all_sets()}
        if not allowed_set_codes:
            print("No sets cached yet — run `python scripts/sync_sets.py` first.")
            return 0
        print(f"{len(allowed_set_codes)} sets allowed (from the sets table)...")

        total = 0
        skipped = 0
        batch: list[tuple] = []

        async def flush() -> None:
            nonlocal total, batch
            if batch:
                total += await database.upsert_cards(batch)
                print(f"  upserted {total}")
                batch = []

        cards_iter = (json.loads(line) for line in data.splitlines() if line.strip()) if is_jsonl else json.loads(data)
        for card in cards_iter:
            if not _wanted(card, allowed_set_codes):
                continue
            row = _safe_to_row(card)
            if row is None:
                skipped += 1
                continue
            batch.append(row)
            if len(batch) >= CHUNK_SIZE:
                await flush()

        await flush()
        if skipped:
            print(f"Skipped {skipped} cards missing required fields.")
        return total
    finally:
        await database.close_db()


def main() -> None:
    count = asyncio.run(sync_all_cards())
    print(f"Cached {count} English card printings.")


if __name__ == "__main__":
    main()
