import os

from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DEV_GUILD_ID = os.getenv("DEV_GUILD_ID") or None
DATABASE_URL = os.getenv("DATABASE_URL")
DEFAULT_SET_CODE = (os.getenv("DEFAULT_SET_CODE") or "").lower() or None

PACK_COOLDOWN_PLAY_SECONDS = int(os.getenv("PACK_COOLDOWN_PLAY_SECONDS", 4 * 3600))
PACK_COOLDOWN_COLLECTOR_SECONDS = int(os.getenv("PACK_COOLDOWN_COLLECTOR_SECONDS", 8 * 3600))

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Point it at your Neon/Supabase Postgres connection string."
    )
