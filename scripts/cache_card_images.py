"""Download every cached card's image into IMAGE_CACHE_DIR right now.
Optional: the bot already does this by itself on startup and then every
IMAGE_CACHE_REFRESH_HOURS (default weekly) — this is for kicking off a
fill manually, e.g. right after a card sync. Safe to re-run — images
already on disk are skipped.

Usage (inside the Docker container, so the cache lands on the mounted volume):
    docker compose run --rm bot python scripts/cache_card_images.py
    docker compose run --rm bot python scripts/cache_card_images.py --set ecl
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from utils import database, pack_image  # noqa: E402

# Faster than the bot's own background refresh, since nothing else is
# competing for bandwidth while this runs.
CONCURRENCY = 8


async def cache_images(set_code: str | None) -> None:
    await database.init_db()
    try:
        await pack_image.fill_cache(set_code, concurrency=CONCURRENCY)
    finally:
        await database.close_db()


def main() -> None:
    parser = argparse.ArgumentParser(description="Download card images into IMAGE_CACHE_DIR.")
    parser.add_argument("--set", dest="set_code", help="Only this set code, e.g. ecl (default: every cached set)")
    args = parser.parse_args()

    if not config.IMAGE_CACHE_DIR:
        sys.exit("IMAGE_CACHE_DIR isn't set — nothing to cache into. See .env.example.")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(cache_images(args.set_code))


if __name__ == "__main__":
    main()
