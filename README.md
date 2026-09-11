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
| `/ripbooster [set]` | Opens your free periodic Play Booster and adds the pulled cards to your collection. Omit `set` for a random eligible set. |
| `/ripcollector [set]` | Same, but a Collector Booster. Omit `set` for a random eligible set. |
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
  packs.py                /ripbooster, /ripcollector
  collection.py           /collection, /missing
  tradeup.py              /tradeup
utils/                  Business logic and data access, independent of Discord (the "service" layer).
  database.py             All SQL — the only module that talks to Postgres directly.
  packs.py                Booster-pack simulation (rarity odds, foils, special treatments, bonus slots).
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
   | `PACK_COOLDOWN_PLAY_SECONDS` / `PACK_COOLDOWN_COLLECTOR_SECONDS` | no | Override the free-pack cooldowns (defaults: 4h / 8h). Handy to set both to `0` while testing. |
   | `DB_POOL_MIN_SIZE` / `DB_POOL_MAX_SIZE` | no | Postgres connection pool sizing (defaults: 2 / 20). |

5. **Load card data.** The database schema is created automatically the
   first time the bot (or any script) calls `database.init_db()`, but the
   `cards`/`sets` tables start empty — you need to populate them from
   Scryfall before `/ripbooster`/`/ripcollector` have anything to pull from:

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

## How booster packs work

See `utils/packs.py` for the exact odds; in short:

- **Play Booster** (15 cards): 14 cards independently rolled by rarity from
  `PLAY_RARITY_WEIGHTS`, plus 1 basic land (omitted if the set has none
  cached). 2 of the 14 each get a chance to be upgraded to a
  special-treatment printing (extended art / showcase / borderless) of the
  same card.
- **Collector Booster** (15 cards): 14 cards from a richer rarity
  distribution (`COLLECTOR_RARITY_WEIGHTS`), plus a land (prefers full-art
  if this set has one cached; omitted entirely if it has no basic lands at
  all). 5 of the 14 each get a (higher) chance at a special-treatment
  upgrade, and each of the 14 independently has a chance to be swapped for
  a card from the set's bonus-linked Commander deck instead (see below).
- Rarity odds are automatically restricted to rarities the set actually has
  cards for — e.g. sets printed before 2008 have no mythics, so a booster
  from one of those never rolls (and silently drops) a mythic slot.
- No foils — every pull is presentation-plain; this bot doesn't model foil odds.
- **Commander decks** (`set_type = 'commander'` with a `parent_set_code`,
  e.g. "Tarkir: Dragonstorm Commander" → "Tarkir: Dragonstorm") have no
  booster of their own — `/ripbooster`/`/ripcollector` resolve straight past
  them to the base set, and the deck's cards are only reachable as that
  bonus pull from the base set's Collector Boosters. Omitting `set` on
  either command picks a random set that's actually openable (cards
  already cached, and not a Commander deck) — see
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
sync. Setting both `PACK_COOLDOWN_*_SECONDS` to `0` is also useful so you
can repeatedly `/ripbooster`/`/ripcollector` without waiting out the cooldown.
