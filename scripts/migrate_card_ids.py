"""One-time migration: convert cards.id from Scryfall's UUID to a compact
integer surrogate key. UUID is 16 bytes; the id gets copied into every
collection and trades row, so shrinking it to a 4-byte int has real,
compounding payoff at scale. Scryfall's original UUID is preserved as
scryfall_id (unique) so future syncs can still upsert by it.

Safe to re-run — it's a no-op if already migrated. Runs as one transaction:
either the whole thing lands or nothing does.

Usage:
    python scripts/migrate_card_ids.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from utils import database  # noqa: E402


async def migrate() -> None:
    await database.init_db()
    pool = database._get_pool()
    try:
        already_migrated = await pool.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'cards' AND column_name = 'scryfall_id')"
        )
        if already_migrated:
            print("Already migrated — nothing to do.")
            return

        cards_before = await pool.fetchval("SELECT COUNT(*) FROM cards")
        collection_before = await pool.fetchval("SELECT COUNT(*) FROM collection")
        trades_before = await pool.fetchval("SELECT COUNT(*) FROM trades")
        print(f"Before: {cards_before} cards, {collection_before} collection rows, {trades_before} trades")

        async with pool.acquire() as conn:
            async with conn.transaction():
                # 1. Rename the UUID id to scryfall_id, drop its PK.
                await conn.execute("ALTER TABLE cards RENAME COLUMN id TO scryfall_id")
                await conn.execute("ALTER TABLE cards DROP CONSTRAINT cards_pkey")

                # 2. Add an integer surrogate id backed by a sequence; make it the PK.
                await conn.execute("CREATE SEQUENCE IF NOT EXISTS cards_id_seq")
                await conn.execute(
                    "ALTER TABLE cards ADD COLUMN id INTEGER NOT NULL DEFAULT nextval('cards_id_seq')"
                )
                await conn.execute("ALTER SEQUENCE cards_id_seq OWNED BY cards.id")
                await conn.execute("ALTER TABLE cards ADD PRIMARY KEY (id)")
                await conn.execute("ALTER TABLE cards ADD CONSTRAINT cards_scryfall_id_key UNIQUE (scryfall_id)")

                # 3. Remap collection.card_id from UUID to the new integer id.
                await conn.execute("ALTER TABLE collection ADD COLUMN card_id_new INTEGER")
                await conn.execute(
                    "UPDATE collection SET card_id_new = cards.id "
                    "FROM cards WHERE cards.scryfall_id = collection.card_id"
                )
                unmatched = await conn.fetchval("SELECT COUNT(*) FROM collection WHERE card_id_new IS NULL")
                if unmatched:
                    raise RuntimeError(f"{unmatched} collection rows didn't match a card by scryfall_id — aborting")
                await conn.execute("ALTER TABLE collection DROP CONSTRAINT collection_pkey")
                await conn.execute("ALTER TABLE collection DROP COLUMN card_id")
                await conn.execute("ALTER TABLE collection RENAME COLUMN card_id_new TO card_id")
                await conn.execute("ALTER TABLE collection ALTER COLUMN card_id SET NOT NULL")
                await conn.execute("ALTER TABLE collection ADD PRIMARY KEY (guild_id, user_id, card_id)")

                # 4. Remap trades.offer_card_id / request_card_id likewise.
                await conn.execute("ALTER TABLE trades ADD COLUMN offer_card_id_new INTEGER")
                await conn.execute(
                    "UPDATE trades SET offer_card_id_new = cards.id "
                    "FROM cards WHERE cards.scryfall_id = trades.offer_card_id"
                )
                await conn.execute("ALTER TABLE trades DROP COLUMN offer_card_id")
                await conn.execute("ALTER TABLE trades RENAME COLUMN offer_card_id_new TO offer_card_id")
                await conn.execute("ALTER TABLE trades ALTER COLUMN offer_card_id SET NOT NULL")

                await conn.execute("ALTER TABLE trades ADD COLUMN request_card_id_new INTEGER")
                await conn.execute(
                    "UPDATE trades SET request_card_id_new = cards.id "
                    "FROM cards WHERE cards.scryfall_id = trades.request_card_id"
                )
                await conn.execute("ALTER TABLE trades DROP COLUMN request_card_id")
                await conn.execute("ALTER TABLE trades RENAME COLUMN request_card_id_new TO request_card_id")
                await conn.execute("ALTER TABLE trades ALTER COLUMN request_card_id SET NOT NULL")

        cards_after = await pool.fetchval("SELECT COUNT(*) FROM cards")
        collection_after = await pool.fetchval("SELECT COUNT(*) FROM collection")
        trades_after = await pool.fetchval("SELECT COUNT(*) FROM trades")
        print(f"After:  {cards_after} cards, {collection_after} collection rows, {trades_after} trades")
        assert (cards_before, collection_before, trades_before) == (cards_after, collection_after, trades_after)
        print("Migration complete — row counts match.")
    finally:
        await database.close_db()


if __name__ == "__main__":
    asyncio.run(migrate())
