"""Central configuration, loaded once at import time from environment
variables (via a `.env` file — see `.env.example` for the full list with
descriptions). Every other module reads settings from here rather than
calling `os.getenv` directly, so there's a single place that knows what
variables exist and what their defaults are.

Importing this module has a side effect: it validates that the required
variables are present and raises immediately if not, so misconfiguration
fails fast at startup instead of surfacing as a confusing error later.
"""

import os

from dotenv import load_dotenv

load_dotenv()

# --- Required ----------------------------------------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# --- Optional ------------------------------------------------------------
# Guild to instantly sync slash commands to during development. Leave unset
# to sync globally instead (propagates to every server, but can take up to
# an hour). See Urza.setup_hook in bot.py.
DEV_GUILD_ID = os.getenv("DEV_GUILD_ID") or None

# How often each user can claim a free booster pack, per server. See
# utils/database.py's PACK_COOLDOWNS and claim_free_pack.
PACK_COOLDOWN_SECONDS = int(os.getenv("PACK_COOLDOWN_SECONDS", 4 * 3600))

# Directory to keep downloaded card images in (see utils/pack_image.py).
# Unset = no disk cache; images are downloaded from Scryfall every time.
# docker-compose.yml points this at a mounted volume.
IMAGE_CACHE_DIR = os.getenv("IMAGE_CACHE_DIR") or None

# When IMAGE_CACHE_DIR is set, the bot downloads every missing card image
# on startup and then again every this many hours (default weekly).
IMAGE_CACHE_REFRESH_HOURS = float(os.getenv("IMAGE_CACHE_REFRESH_HOURS") or 7 * 24)

# Custom status shown under the bot's name in the member list.
BOT_STATUS = os.getenv("BOT_STATUS") or "🃏 Shuffling 70,000 cards"

# Postgres connection pool bounds (see utils.database.init_db). Neon/Supabase
# pooled endpoints comfortably support more than this default; raise it if
# you deploy to many concurrent guilds and see pool-exhaustion warnings.
DB_POOL_MIN_SIZE = int(os.getenv("DB_POOL_MIN_SIZE", 2))
DB_POOL_MAX_SIZE = int(os.getenv("DB_POOL_MAX_SIZE", 20))

# --- Fail fast on missing required config --------------------------------
if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Point it at your Neon/Supabase Postgres connection string."
    )
