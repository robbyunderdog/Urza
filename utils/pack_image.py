"""Render a whole opened pack as one grid image, so the result can be shown
as a single upload instead of one card image per page. No Discord code
lives here — cogs/packs.py wraps the returned bytes in a discord.File.

Card art comes from Scryfall's image CDN (cards.scryfall.io), downloaded
in parallel over one long-lived HTTP session (opening a fresh TLS
connection per pack cost ~150ms). If IMAGE_CACHE_DIR is set, each image is
also kept on disk the first time it's downloaded, so repeat cards skip the
download entirely — see cache_path for the layout. fill_cache downloads
everything missing in one go; the bot runs it on startup and then every
IMAGE_CACHE_REFRESH_HOURS (see bot.py).
"""

import asyncio
import io
import logging
import os
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from PIL import Image, ImageDraw

import config
from utils.packs import PulledCard

log = logging.getLogger("urza")

GRID_COLUMNS = 5
THUMB_WIDTH = 244  # half of Scryfall's 488px "normal" size
THUMB_HEIGHT = 340
GAP = 8
BACKGROUND = (43, 45, 49)  # Discord's dark-theme message background
PLACEHOLDER = (70, 72, 78)
JPEG_QUALITY = 85
DOWNLOAD_TIMEOUT_SECONDS = 10

# Scryfall asks every client to send a descriptive User-Agent and Accept.
HEADERS = {"User-Agent": "UrzaDiscordBot/1.0", "Accept": "image/*"}

_session: aiohttp.ClientSession | None = None
_cache_write_warned = False


def new_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(headers=HEADERS, timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT_SECONDS))


def _get_session() -> aiohttp.ClientSession:
    """The bot's shared Scryfall session, created on first use."""
    global _session
    if _session is None or _session.closed:
        _session = new_session()
    return _session


async def close() -> None:
    """Close the shared session — called from Urza.close in bot.py."""
    if _session is not None and not _session.closed:
        await _session.close()


def cache_path(set_code: str | None, image_url: str) -> Path | None:
    """Where this image lives in the on-disk cache, or None if caching is
    off. Layout: <IMAGE_CACHE_DIR>/<set_code>/<scryfall_id>-<version>.jpg.
    Scryfall's image URLs end in "<scryfall_id>.jpg?<version>", where the
    version changes whenever Scryfall replaces the image (e.g. a preview
    scan swapped for a high-res one), so a new version is a new file
    rather than a stale cached one."""
    if not config.IMAGE_CACHE_DIR:
        return None
    parts = urlsplit(image_url)
    scryfall_id = Path(parts.path).stem
    version = parts.query or "0"
    return Path(config.IMAGE_CACHE_DIR) / (set_code or "_other") / f"{scryfall_id}-{version}.jpg"


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def write_atomic(path: Path, data: bytes) -> None:
    """Write via a temp file + rename, so a crash or a concurrent reader
    never sees a half-written image."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


async def download(session: aiohttp.ClientSession, url: str) -> bytes:
    async with session.get(url) as resp:
        resp.raise_for_status()
        return await resp.read()


async def save_to_cache(path: Path, data: bytes) -> None:
    """Best-effort cache write — a full or read-only cache directory (e.g.
    the NAS share's permissions don't let the container user write) is
    logged once and otherwise ignored, never failing the pack."""
    global _cache_write_warned
    try:
        await asyncio.to_thread(write_atomic, path, data)
    except OSError as e:
        if not _cache_write_warned:
            log.warning("Can't write to image cache %s (%s) — continuing without caching", config.IMAGE_CACHE_DIR, e)
            _cache_write_warned = True


async def _load(card: PulledCard) -> bytes | None:
    """One card's image bytes: from the disk cache if present, otherwise
    downloaded (and then cached). None if there's no URL or the download
    fails — a single missing image becomes a placeholder tile rather than
    failing the whole pack."""
    if not card.image_url:
        return None
    path = cache_path(card.set_code, card.image_url)
    if path is not None:
        data = await asyncio.to_thread(_read, path)
        if data is not None:
            return data
    try:
        data = await download(_get_session(), card.image_url)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None
    if path is not None:
        await save_to_cache(path, data)
    return data


def _missing_images(rows) -> list[tuple[str, Path]]:
    """(url, cache path) for each row whose image isn't on disk yet. Lists
    each set's folder once instead of checking every file — much faster
    when the cache lives on a network share. Blocking; run in a thread."""
    existing: dict[Path, set[str]] = {}
    missing = []
    for row in rows:
        path = cache_path(row["set_code"], row["image_url"])
        if path.parent not in existing:
            existing[path.parent] = set(os.listdir(path.parent)) if path.parent.is_dir() else set()
        if path.name not in existing[path.parent]:
            missing.append((row["image_url"], path))
    return missing


async def fill_cache(set_code: str | None = None, concurrency: int = 4) -> tuple[int, int]:
    """Download every cached card's image that isn't on disk yet (or just
    one set's), `concurrency` at a time. Returns (downloaded, failed).
    Used by the bot's periodic refresh (bot.py) and by
    scripts/cache_card_images.py. Images already cached are skipped, so
    after the first full run this only fetches cards added by a sync, or
    images Scryfall has since replaced (a new version in the file name)."""
    if not config.IMAGE_CACHE_DIR:
        return 0, 0
    from utils import database  # local import: rendering a pack doesn't need it

    rows = await database.get_image_urls(set_code)
    todo = await asyncio.to_thread(_missing_images, rows)
    log.info("Image cache: %d images in scope, %d to download", len(rows), len(todo))
    if not todo:
        return 0, 0

    queue = iter(todo)  # shared by the workers, so only `concurrency` downloads exist at once
    done = failed = 0
    started = time.monotonic()

    async def worker(session: aiohttp.ClientSession) -> None:
        nonlocal done, failed
        for url, path in queue:
            try:
                await asyncio.to_thread(write_atomic, path, await download(session, url))
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                failed += 1
                log.warning("Image cache: failed %s (%s)", url, e)
            done += 1
            if done % 1000 == 0:
                rate = done / (time.monotonic() - started)
                log.info(
                    "Image cache: %d/%d (%.0f/s, ~%.0f min left)", done, len(todo), rate, (len(todo) - done) / rate / 60
                )

    async with new_session() as session:
        await asyncio.gather(*(worker(session) for _ in range(concurrency)))
    log.info("Image cache: %d downloaded, %d failed, in %.1f min", done - failed, failed, (time.monotonic() - started) / 60)
    return done - failed, failed


def _placeholder(name: str) -> Image.Image:
    """A plain tile with the card's name, for cards with no usable image."""
    tile = Image.new("RGB", (THUMB_WIDTH, THUMB_HEIGHT), PLACEHOLDER)
    ImageDraw.Draw(tile).text((10, THUMB_HEIGHT // 2), name, fill=(220, 221, 222))
    return tile


def _compose(cards: list[PulledCard], images: list[bytes | None]) -> bytes:
    """Lay the cards out left-to-right, top-to-bottom in a GRID_COLUMNS-wide
    grid and encode as JPEG. CPU-bound — run via asyncio.to_thread."""
    rows = -(-len(cards) // GRID_COLUMNS)
    width = GRID_COLUMNS * THUMB_WIDTH + (GRID_COLUMNS + 1) * GAP
    height = rows * THUMB_HEIGHT + (rows + 1) * GAP
    canvas = Image.new("RGB", (width, height), BACKGROUND)

    for i, (card, data) in enumerate(zip(cards, images)):
        try:
            tile = Image.open(io.BytesIO(data)).convert("RGB").resize((THUMB_WIDTH, THUMB_HEIGHT), Image.LANCZOS)
        except Exception:
            tile = _placeholder(card.name)
        col, row = i % GRID_COLUMNS, i // GRID_COLUMNS
        canvas.paste(tile, (GAP + col * (THUMB_WIDTH + GAP), GAP + row * (THUMB_HEIGHT + GAP)))

    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=JPEG_QUALITY)
    return out.getvalue()


async def render_pack_grid(cards: list[PulledCard]) -> bytes:
    """Load every card's image in parallel and return the composited grid
    as JPEG bytes, in the order `cards` is given."""
    images = await asyncio.gather(*(_load(card) for card in cards))
    return await asyncio.to_thread(_compose, cards, list(images))
