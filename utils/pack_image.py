"""Render a whole opened pack as one grid image, so the result can be shown
as a single upload instead of one card image per page. No Discord code
lives here — cogs/packs.py wraps the returned bytes in a discord.File.

Card art is downloaded from Scryfall's image CDN (cards.scryfall.io) at
render time, all cards in parallel, and composited with Pillow. Nothing is
cached on disk.
"""

import asyncio
import io

import aiohttp
from PIL import Image, ImageDraw

from utils.packs import PulledCard

GRID_COLUMNS = 5
THUMB_WIDTH = 244  # half of Scryfall's 488px "normal" size
THUMB_HEIGHT = 340
GAP = 8
BACKGROUND = (43, 45, 49)  # Discord's dark-theme message background
PLACEHOLDER = (70, 72, 78)
JPEG_QUALITY = 85
DOWNLOAD_TIMEOUT_SECONDS = 10

# Scryfall asks every client to send a descriptive User-Agent and Accept.
_HEADERS = {"User-Agent": "UrzaDiscordBot/1.0", "Accept": "image/*"}


async def _download(session: aiohttp.ClientSession, url: str | None) -> bytes | None:
    """Fetch one card image, or None if there's no URL or the download
    fails — a single missing image becomes a placeholder tile rather than
    failing the whole pack."""
    if not url:
        return None
    try:
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.read()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None


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
    """Download every card's image in parallel and return the composited
    grid as JPEG bytes, in the order `cards` is given."""
    timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(headers=_HEADERS, timeout=timeout) as session:
        images = await asyncio.gather(*(_download(session, card.image_url) for card in cards))
    return await asyncio.to_thread(_compose, cards, list(images))
