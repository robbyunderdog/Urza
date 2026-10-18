# Urza — MTG Pack-Opener Discord Bot

Urza is a Discord bot that lets a server's members open simulated *Magic: The
Gathering* booster packs, build up a per-server card collection, trade
duplicates for higher rarities, and track completion progress toward a full
set. All card and set data is sourced from the [Scryfall API](https://scryfall.com/docs/api)
and cached in Postgres, so day-to-day commands never call out to Scryfall
directly — only the sync scripts do.

## Features

| Command | What it does |
|---|---|
| `/rip [set]` | Opens your free periodic pack, adds the pulled cards to your collection, and shows the whole pack as one image ("View cards" flips through it full-size). Omit `set` for a random eligible set. |
| `/collection [set]` | With no `set`: paginated overview of your progress across every cached set. With `set`: a per-rarity breakdown for that one set. |
| `/missing [set] [rarity]` | With no `set`: total missing cards and overall completion % across every cached set. With `set`: lists exactly which cards you're missing (optionally filtered to one rarity). |
| `/tradeup <set> <rarity>` | Spends duplicate copies of one rarity for a random card of the next rarity up. |
| `/ping` | Health check — replies with the bot's gateway latency. |

Collections, cooldowns, and trades are all scoped **per Discord server**
(`guild_id`) — the same user has an independent collection in every server
the bot is in.

## Project layout

```
bot.py                  Entry point: builds the bot, loads cogs, syncs slash commands.
config.py               Loads and validates environment variables (.env).
cogs/                   One file per Discord-facing command group (the "controller" layer).
  general.py              /ping
  packs.py                /rip
  collection.py           /collection, /missing
  tradeup.py              /tradeup
utils/                  Business logic and data access, independent of Discord (the "service" layer).
  database.py             All SQL — the only module that talks to Postgres directly.
  packs.py                Booster-pack simulation (rarity odds, pack pool).
  pack_image.py           Renders an opened pack as one grid image (Pillow).
  sets.py                 Free-text set-name resolution ("mh3" / "Modern Horizons 3" -> a sets row) + in-memory cache.
  tradeup.py              Trade-up rules and validation.
  views.py                Shared discord.ui.View base class with error handling.
db/
  schema.sql              Postgres schema, applied automatically on every bot startup.
scripts/                One-off/periodic maintenance scripts, run manually via `python scripts/<name>.py`.
  sync_sets.py             Cache set metadata from Scryfall (run this first).
  sync_all_cards.py        Cache every card printing via Scryfall's bulk-data export (recommended full sync).
  sync_set.py              Cache a single set's cards via Scryfall's search API (faster for one set at a time).
  prune_minor_sets.py      Delete cached cards/sets outside the curated "major set" list.
  migrate_card_ids.py      One-time migration from UUID card ids to compact integer ids.
  cache_card_images.py     Download every missing card image into IMAGE_CACHE_DIR now (the bot also does this on startup and weekly).
```

### Architecture: cogs vs. utils

- **`cogs/`** owns everything Discord-specific: slash-command definitions,
  argument parsing/validation, `discord.Embed`/`discord.ui.View` presentation,
  and turning internal exceptions into user-facing messages. A cog method
  should read like a thin script: defer the interaction, call into `utils/`,
  format the result, send it.
- **`utils/`** owns the actual rules — pack odds, trade-up costs, SQL
  queries — with no dependency on `discord`. This split means the pack
  simulation, database layer, etc. could be unit-tested or reused (e.g. in a
  CLI) without spinning up a Discord connection.

## Setup

1. **Install dependencies** (Python 3.11+; the code uses `X | None` union
   syntax and `datetime.timezone.utc`):

   ```bash
   python -m venv .venv
   .venv/Scripts/activate      # Windows
   # source .venv/bin/activate # macOS/Linux
   pip install -r requirements.txt
   ```

2. **Create a Discord application/bot** at the
   [Discord Developer Portal](https://discord.com/developers/applications),
   copy its bot token, and invite it to your server with the `applications.commands`
   and `bot` scopes (no privileged intents are required — the bot only uses
   slash commands).

3. **Provision a Postgres database.** Any Postgres works; [Neon](https://neon.tech)
   or [Supabase](https://supabase.com) both offer a free pooled Postgres
   endpoint that this bot is specifically compatible with (see the
   `statement_cache_size=0` note in `utils/database.py` — required for
   pgbouncer transaction-mode pooling).

4. **Configure environment variables** — copy `.env.example` to `.env` and
   fill in at least `DISCORD_TOKEN` and `DATABASE_URL`:

   | Variable | Required | Purpose |
   |---|---|---|
   | `DISCORD_TOKEN` | yes | Bot token from the Developer Portal. |
   | `DATABASE_URL` | yes | Postgres connection string. |
   | `DEV_GUILD_ID` | no | Set during development for instant slash-command sync to one guild. Global sync (empty) can take up to an hour to propagate but works everywhere. |
   | `PACK_COOLDOWN_SECONDS` | no | Override the free-pack cooldown (default: 4h). Handy to set to `0` while testing. |
   | `IMAGE_CACHE_DIR` | no | Directory to keep downloaded card images in, so repeat cards skip the Scryfall download. Unset = no cache. `docker-compose.yml` sets this itself. |
   | `IMAGE_CACHE_REFRESH_HOURS` | no | How often the bot downloads missing card images into `IMAGE_CACHE_DIR` (default: 168 = weekly; also runs on startup). |
   | `DB_POOL_MIN_SIZE` / `DB_POOL_MAX_SIZE` | no | Postgres connection pool sizing (defaults: 2 / 20). |

5. **Load card data.** The database schema is created automatically the
   first time the bot (or any script) calls `database.init_db()`, but the
   `cards`/`sets` tables start empty — you need to populate them from
   Scryfall before `/rip` has anything to pull from:

   ```bash
   python scripts/sync_sets.py        # set metadata — always run this first
   python scripts/sync_all_cards.py   # full card sync (slow, thorough — recommended)
   # or, to sync just one set instead of everything:
   python scripts/sync_set.py mkm
   ```

   Re-run `sync_sets.py` periodically (it's cheap) to pick up newly
   announced sets, and `sync_all_cards.py` / `sync_set.py` again to pick up
   new cards or corrections.

6. **Run the bot:**

   ```bash
   python bot.py
   ```

   On startup it applies `db/schema.sql` (idempotent — safe to run every
   time), loads each cog in `EXTENSIONS`, and syncs slash commands (to
   `DEV_GUILD_ID` if set, otherwise globally).

   **Or with Docker**, once `.env` is filled in (step 4) and card data is
   loaded (step 5 — run those scripts on the host, against the same
   `DATABASE_URL`, since they're one-off maintenance tasks rather than part
   of the bot's own process):

   ```bash
   docker compose up --build -d   # build the image and run the bot in the background
   docker compose logs -f         # follow logs
   docker compose down            # stop it
   ```

   This builds from the included `Dockerfile` and reads `.env` via
   `docker-compose.yml`'s `env_file`. There's no database container —
   `DATABASE_URL` still points at your external Neon/Supabase Postgres.
   Without Compose, the equivalent is:

   ```bash
   docker build -t urza-bot .
   docker run -d --name urza-bot --env-file .env --restart unless-stopped urza-bot
   ```

   **Card image cache.** `docker-compose.yml` mounts
   `/mnt/truenas-share/data/urza/card-images` on the host (override with
   `IMAGE_CACHE_HOST_DIR` in your shell or a Compose `.env`) at
   `/data/card-images` in the container, and points `IMAGE_CACHE_DIR` at it.
   The container runs as uid 1000, so that directory must exist and be
   writable by uid 1000 before starting — otherwise Docker creates it as
   root, and the bot logs a warning and runs without caching. Images are
   stored as `<set_code>/<scryfall_id>-<version>.jpg` (~6.5 GB for every
   card). The bot downloads every missing image on startup and then every
   `IMAGE_CACHE_REFRESH_HOURS` (default weekly) in the background — the
   first run takes a while, later ones only fetch what's new. To kick off
   a fill by hand, e.g. right after a card sync:

   ```bash
   docker compose run --rm bot python scripts/cache_card_images.py   # every set, resumable
   docker compose run --rm bot python scripts/cache_card_images.py --set ecl
   ```

## How booster packs work

See `utils/packs.py` for the exact odds; in short:

- **One pack type** (15 cards): 14 cards independently rolled by rarity
  from `PACK_RARITY_WEIGHTS`, plus 1 basic land (prefers full-art; omitted
  if the set has none cached).
- The pool is **every non-basic printing in the set**, alternate-art
  printings included — every printing of a given rarity has an equal chance.
  Alternate art (extended art, showcase, inverted, borderless, full art) is
  marked 🎨 when shown, but isn't weighted any differently.
- Rarity odds are automatically restricted to rarities the set actually has
  cards for — e.g. sets printed before 2008 have no mythics, so a booster
  from one of those never rolls (and silently drops) a mythic slot.
- No foils — every pull is presentation-plain; this bot doesn't model foil odds.
- **Commander decks** (`set_type = 'commander'` with a `parent_set_code`,
  e.g. "Tarkir: Dragonstorm Commander" → "Tarkir: Dragonstorm") are opened
  and completed as their own set, e.g. `/rip set:Tarkir: Dragonstorm Commander`.
  Omitting `set` picks a random set with cards already cached — see
  `database.get_random_open_eligible_set`.

## Database schema

Defined in `db/schema.sql` (applied automatically, safe to re-run):

- **`cards`** — one row per unique printing (not per card — two art
  variants of the same card are two rows), cached from Scryfall. Keyed by a
  compact integer `id` (not Scryfall's UUID) since that id is duplicated
  into every `collection`/`trades` row.
- **`sets`** — set metadata used for free-text set resolution (`utils/sets.py`).
- **`collection`** — `(guild_id, user_id, card_id) -> quantity`. Per-server,
  per-user, per-printing ownership counts.
- **`pack_cooldowns`** — `(guild_id, user_id, booster_type) -> last_opened_at`,
  enforcing the free-pack timers.
- **`trades`** — pending/accepted/declined/cancelled player-to-player trade
  offers, scoped to the server they were made in.

## Testing changes locally

There's no automated test suite; verify changes by running the bot against
a real (or disposable dev) Discord server and Postgres database:

```bash
python bot.py
```

Set `DEV_GUILD_ID` in `.env` while iterating — slash-command changes sync to
that one guild in seconds instead of waiting up to an hour for a global
sync. Setting `PACK_COOLDOWN_SECONDS` to `0` is also useful so you can
repeatedly `/rip` without waiting out the cooldown.
