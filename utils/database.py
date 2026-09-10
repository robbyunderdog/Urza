from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg

import config

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "db" / "schema.sql"

# Frame effects that mark a cosmetic alternate-art printing (not mechanical
# ones like "legendary" or "nyxtouched", which apply to every printing of
# that card type). Not exhaustive on their own — many borderless cards (e.g.
# borderless planeswalkers) carry no frame_effects at all and are only
# distinguishable by border_color, so every special-treatment check below
# also checks border_color = 'borderless' independently.
SPECIAL_TREATMENT_FRAMES = ["extendedart", "showcase", "inverted"]
_SPECIAL_TREATMENT_SQL = "(frame_effects && {0}::text[] OR border_color = 'borderless')"

_pool: asyncpg.Pool | None = None


async def init_db() -> None:
    global _pool
    if _pool is None:
        # statement_cache_size=0: required for pgbouncer transaction-mode
        # poolers (Neon's pooled endpoint, Supabase's port-6543 pooler).
        # Harmless on a direct connection too, so it's left on unconditionally.
        _pool = await asyncpg.create_pool(
            config.DATABASE_URL,
            min_size=config.DB_POOL_MIN_SIZE,
            max_size=config.DB_POOL_MAX_SIZE,
            statement_cache_size=0,
        )
    async with _pool.acquire() as conn:
        await conn.execute(SCHEMA_PATH.read_text())


async def close_db() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def _get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialized; call init_db() first.")
    return _pool


# ---------------------------------------------------------------------------
# Card cache
# ---------------------------------------------------------------------------


async def upsert_cards(rows: list[tuple]) -> int:
    """Bulk insert/update printing rows. Each row is a tuple matching:
    (scryfall_id, oracle_id, name, set_code, collector_number, rarity,
    is_basic_land, frame_effects, finishes, full_art, border_color,
    mana_cost, type_line, image_url). The internal integer `id` (what
    collection/trades actually reference) is assigned automatically on
    first insert and never touched on update, so it stays stable across
    re-syncs.
    """
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO cards (
                scryfall_id, oracle_id, name, set_code, collector_number, rarity, is_basic_land,
                frame_effects, finishes, full_art, border_color, mana_cost, type_line, image_url
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
            ON CONFLICT (scryfall_id) DO UPDATE SET
                oracle_id = EXCLUDED.oracle_id,
                name = EXCLUDED.name,
                set_code = EXCLUDED.set_code,
                collector_number = EXCLUDED.collector_number,
                rarity = EXCLUDED.rarity,
                is_basic_land = EXCLUDED.is_basic_land,
                frame_effects = EXCLUDED.frame_effects,
                finishes = EXCLUDED.finishes,
                full_art = EXCLUDED.full_art,
                border_color = EXCLUDED.border_color,
                mana_cost = EXCLUDED.mana_cost,
                type_line = EXCLUDED.type_line,
                image_url = EXCLUDED.image_url
            """,
            rows,
        )
    return len(rows)


async def set_is_cached(set_code: str) -> bool:
    pool = _get_pool()
    return await pool.fetchval("SELECT EXISTS (SELECT 1 FROM cards WHERE set_code = $1)", set_code)


async def get_random_cards(
    set_code: str,
    rarity: str | list[str],
    count: int,
    exclude_basic_land: bool = False,
    exclude_special_treatment: bool = False,
) -> list[asyncpg.Record]:
    """Draw random printings. exclude_special_treatment=True restricts to
    "base" printings (no extended-art/showcase frame) — pack opening draws
    the base version first, then decides separately whether to upgrade a
    specific pull to a special-treatment printing of that same card (see
    get_special_treatment_printing), rather than treating a special
    printing as just another random member of the normal pool."""
    rarities = [rarity] if isinstance(rarity, str) else list(rarity)
    query = (
        "SELECT id, oracle_id, name, rarity, image_url, finishes FROM cards "
        "WHERE set_code = $1 AND rarity = ANY($2::text[])"
    )
    params: list = [set_code, rarities]
    if exclude_basic_land:
        query += " AND is_basic_land = FALSE"
    if exclude_special_treatment:
        placeholder = f"${len(params) + 1}"
        query += f" AND NOT {_SPECIAL_TREATMENT_SQL.format(placeholder)}"
        params.append(SPECIAL_TREATMENT_FRAMES)
    query += f" ORDER BY RANDOM() LIMIT ${len(params) + 1}"
    params.append(count)

    pool = _get_pool()
    return await pool.fetch(query, *params)


async def get_special_treatment_printing(oracle_id: str, set_code: str) -> asyncpg.Record | None:
    """A special-treatment printing (extended art, showcase, borderless,
    etc. — see SPECIAL_TREATMENT_FRAMES) of this exact card (same
    oracle_id) within this set, if one exists — used to "upgrade" an
    already-drawn base card to its special-treatment version, rather than
    pulling an unrelated special-treatment card from the pool."""
    pool = _get_pool()
    return await pool.fetchrow(
        f"""
        SELECT id, name, rarity, image_url, finishes FROM cards
        WHERE oracle_id = $1 AND set_code = $2 AND {_SPECIAL_TREATMENT_SQL.format("$3")}
        ORDER BY RANDOM() LIMIT 1
        """,
        oracle_id,
        set_code,
        SPECIAL_TREATMENT_FRAMES,
    )


async def get_random_basic_lands(set_code: str, count: int) -> list[asyncpg.Record]:
    """Prefers full-art printings when this set has any cached."""
    pool = _get_pool()
    return await pool.fetch(
        """
        SELECT id, name, rarity, image_url, finishes FROM cards
        WHERE set_code = $1 AND is_basic_land = TRUE
        ORDER BY full_art DESC, RANDOM() LIMIT $2
        """,
        set_code,
        count,
    )


# ---------------------------------------------------------------------------
# Sets (for free-text set name resolution)
# ---------------------------------------------------------------------------


async def upsert_sets(rows: list[tuple]) -> int:
    """Each row: (code, name, set_type, released_at, parent_set_code)."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO sets (code, name, set_type, released_at, parent_set_code)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (code) DO UPDATE SET
                name = EXCLUDED.name, set_type = EXCLUDED.set_type,
                released_at = EXCLUDED.released_at, parent_set_code = EXCLUDED.parent_set_code
            """,
            rows,
        )
    return len(rows)


async def get_bonus_set_code(parent_code: str) -> str | None:
    """The set-specific Commander deck's code for a base set, if any (e.g.
    'dmu' -> 'dmc'). None if that base set has no linked Commander deck."""
    pool = _get_pool()
    return await pool.fetchval(
        "SELECT code FROM sets WHERE set_type = 'commander' AND parent_set_code = $1 LIMIT 1", parent_code
    )


_ALLOWED_CODES_CTE = """
    SELECT code FROM sets WHERE set_type = ANY($1::text[])
    UNION
    SELECT code FROM sets WHERE set_type = 'commander' AND parent_set_code IN (
        SELECT code FROM sets WHERE set_type = ANY($1::text[])
    )
"""


async def prune_non_major_sets(major_set_types: list[str]) -> dict[str, int]:
    """Delete cached cards (and any collection/trade rows referencing them)
    belonging to sets that aren't a major set type and aren't a Commander
    deck linked to one, then delete those sets' metadata rows too. Returns
    counts of what was removed."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            minor_ids = [
                row["id"]
                for row in await conn.fetch(
                    f"SELECT id FROM cards WHERE set_code NOT IN ({_ALLOWED_CODES_CTE})",
                    major_set_types,
                )
            ]

            collection_deleted = trades_deleted = 0
            if minor_ids:
                result = await conn.execute("DELETE FROM collection WHERE card_id = ANY($1::int[])", minor_ids)
                collection_deleted = int(result.split()[-1])
                result = await conn.execute(
                    "DELETE FROM trades WHERE offer_card_id = ANY($1::int[]) OR request_card_id = ANY($1::int[])",
                    minor_ids,
                )
                trades_deleted = int(result.split()[-1])

            cards_result = await conn.execute(
                f"DELETE FROM cards WHERE set_code NOT IN ({_ALLOWED_CODES_CTE})",
                major_set_types,
            )
            sets_result = await conn.execute(
                f"DELETE FROM sets WHERE code NOT IN ({_ALLOWED_CODES_CTE})",
                major_set_types,
            )
            return {
                "cards": int(cards_result.split()[-1]),
                "sets": int(sets_result.split()[-1]),
                "collection_rows": collection_deleted,
                "trade_rows": trades_deleted,
            }


async def get_all_sets() -> list[asyncpg.Record]:
    pool = _get_pool()
    return await pool.fetch(
        "SELECT code, name, set_type, released_at, parent_set_code FROM sets ORDER BY released_at DESC NULLS LAST"
    )


async def get_set_by_code(code: str) -> asyncpg.Record | None:
    pool = _get_pool()
    return await pool.fetchrow(
        "SELECT code, name, set_type, released_at, parent_set_code FROM sets WHERE code = $1", code.lower()
    )


async def get_set_by_name(name: str) -> asyncpg.Record | None:
    pool = _get_pool()
    return await pool.fetchrow(
        "SELECT code, name, set_type, released_at, parent_set_code FROM sets WHERE LOWER(name) = LOWER($1)", name
    )


# ---------------------------------------------------------------------------
# Collection (per-server, per-printing)
# ---------------------------------------------------------------------------


async def add_to_collection(guild_id: int, user_id: int, card_ids: list[int]) -> None:
    counts: dict[int, int] = {}
    for card_id in card_ids:
        counts[card_id] = counts.get(card_id, 0) + 1
    if not counts:
        return

    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO collection (guild_id, user_id, card_id, quantity)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (guild_id, user_id, card_id)
            DO UPDATE SET quantity = collection.quantity + EXCLUDED.quantity
            """,
            [(guild_id, user_id, card_id, qty) for card_id, qty in counts.items()],
        )


async def get_collection(guild_id: int, user_id: int) -> list[asyncpg.Record]:
    """Return (name, rarity, set_code, quantity) per owned printing."""
    pool = _get_pool()
    return await pool.fetch(
        """
        SELECT cards.name, cards.rarity, cards.set_code, collection.quantity
        FROM collection
        JOIN cards ON cards.id = collection.card_id
        WHERE collection.guild_id = $1 AND collection.user_id = $2
        ORDER BY collection.quantity DESC, cards.name ASC
        """,
        guild_id,
        user_id,
    )


async def find_owned_card_by_name(
    guild_id: int, user_id: int, name: str, set_hint: str | None = None, collector_hint: str | None = None
) -> list[asyncpg.Record]:
    """Cards this user owns in this server whose name matches
    (case-insensitive), optionally narrowed to one set and/or collector
    number. Normally returns 0 or 1 rows; more than 1 means it's ambiguous —
    e.g. the user owns the same card from two different printings, which can
    happen even within one set (an extended-art variant is a separate
    printing with its own collector number)."""
    pool = _get_pool()
    return await pool.fetch(
        """
        SELECT collection.card_id, cards.name, cards.rarity, cards.set_code, cards.collector_number
        FROM collection
        JOIN cards ON cards.id = collection.card_id
        WHERE collection.guild_id = $1 AND collection.user_id = $2
          AND LOWER(cards.name) = LOWER($3)
          AND ($4::text IS NULL OR LOWER(cards.set_code) = LOWER($4))
          AND ($5::text IS NULL OR cards.collector_number = $5)
        """,
        guild_id,
        user_id,
        name,
        set_hint,
        collector_hint,
    )


async def count_spendable_duplicates(guild_id: int, user_id: int, set_code: str, rarity: str) -> int:
    """How many duplicate copies (quantity beyond the first) of a given
    set+rarity this user could spend trading up."""
    pool = _get_pool()
    total = await pool.fetchval(
        """
        SELECT COALESCE(SUM(collection.quantity - 1), 0)
        FROM collection
        JOIN cards ON cards.id = collection.card_id
        WHERE collection.guild_id = $1 AND collection.user_id = $2
          AND cards.set_code = $3 AND cards.rarity = $4 AND collection.quantity > 1
        """,
        guild_id,
        user_id,
        set_code,
        rarity,
    )
    return total or 0


async def spend_duplicates(guild_id: int, user_id: int, set_code: str, rarity: str, amount: int) -> int:
    """Atomically spend exactly `amount` duplicate copies of a given
    set+rarity from a user's collection, or none at all if there aren't
    enough — never a partial spend (the sufficiency check and the spend
    happen under the same row lock, closing the race where a concurrent
    call could take duplicates out from under a check that already passed).
    Returns `amount` on success or 0 if there weren't enough."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                """
                SELECT collection.card_id, collection.quantity
                FROM collection
                JOIN cards ON cards.id = collection.card_id
                WHERE collection.guild_id = $1 AND collection.user_id = $2
                  AND cards.set_code = $3 AND cards.rarity = $4 AND collection.quantity > 1
                ORDER BY collection.quantity DESC
                FOR UPDATE OF collection
                """,
                guild_id,
                user_id,
                set_code,
                rarity,
            )
            available = sum(row["quantity"] - 1 for row in rows)
            if available < amount:
                return 0

            remaining = amount
            for row in rows:
                if remaining <= 0:
                    break
                take = min(row["quantity"] - 1, remaining)
                if take <= 0:
                    continue
                await conn.execute(
                    "UPDATE collection SET quantity = quantity - $1 WHERE guild_id = $2 AND user_id = $3 AND card_id = $4",
                    take,
                    guild_id,
                    user_id,
                    row["card_id"],
                )
                remaining -= take
            return amount


async def get_set_progress(guild_id: int, user_id: int, set_code: str) -> dict[str, tuple[int, int]]:
    """{rarity: (owned_printings, total_printings)} for a set, excluding
    basic lands. Printing-level: two art variants of the same card count as
    two separate entries to collect."""
    pool = _get_pool()
    rows = await pool.fetch(
        """
        SELECT cards.rarity,
               COUNT(*) AS total,
               COUNT(collection.card_id) AS owned
        FROM cards
        LEFT JOIN collection ON collection.card_id = cards.id
            AND collection.guild_id = $1 AND collection.user_id = $2
        WHERE cards.set_code = $3 AND cards.is_basic_land = FALSE
        GROUP BY cards.rarity
        """,
        guild_id,
        user_id,
        set_code,
    )
    return {row["rarity"]: (row["owned"], row["total"]) for row in rows}


async def get_missing_cards(
    guild_id: int, user_id: int, set_code: str, rarity: str | None = None
) -> list[asyncpg.Record]:
    pool = _get_pool()
    return await pool.fetch(
        r"""
        SELECT cards.id, cards.name, cards.rarity, cards.collector_number
        FROM cards
        LEFT JOIN collection ON collection.card_id = cards.id
            AND collection.guild_id = $1 AND collection.user_id = $2
        WHERE cards.set_code = $3 AND cards.is_basic_land = FALSE AND collection.card_id IS NULL
          AND ($4::text IS NULL OR cards.rarity = $4)
        ORDER BY (substring(cards.collector_number from '^\d+'))::int NULLS LAST, cards.collector_number
        """,
        guild_id,
        user_id,
        set_code,
        rarity,
    )


async def get_all_sets_progress(guild_id: int, user_id: int) -> list[asyncpg.Record]:
    """(code, name, owned, total) printings per cached set, excluding basic
    lands, most recently released first — including sets the user has 0
    cards from."""
    pool = _get_pool()
    return await pool.fetch(
        """
        SELECT cards.set_code AS code, sets.name AS name, sets.released_at AS released_at,
               COUNT(*) AS total,
               COUNT(collection.card_id) AS owned
        FROM cards
        LEFT JOIN sets ON sets.code = cards.set_code
        LEFT JOIN collection ON collection.card_id = cards.id
            AND collection.guild_id = $1 AND collection.user_id = $2
        WHERE cards.is_basic_land = FALSE
        GROUP BY cards.set_code, sets.name, sets.released_at
        ORDER BY sets.released_at DESC NULLS LAST, cards.set_code ASC
        """,
        guild_id,
        user_id,
    )




# ---------------------------------------------------------------------------
# Pack cooldowns
# ---------------------------------------------------------------------------

PACK_COOLDOWNS = {
    "play": timedelta(seconds=config.PACK_COOLDOWN_PLAY_SECONDS),
    "collector": timedelta(seconds=config.PACK_COOLDOWN_COLLECTOR_SECONDS),
}


async def release_pack_claim(guild_id: int, user_id: int, booster_type: str) -> None:
    """Undo a claim_free_pack claim — used when pack generation fails after
    the cooldown was already claimed, so a transient error doesn't cost the
    user their free pack for nothing."""
    pool = _get_pool()
    await pool.execute(
        "DELETE FROM pack_cooldowns WHERE guild_id = $1 AND user_id = $2 AND booster_type = $3",
        guild_id,
        user_id,
        booster_type,
    )


async def claim_free_pack(guild_id: int, user_id: int, booster_type: str) -> timedelta | None:
    """Try to claim a free pack in this server. Returns None on success (and
    records the claim), or the remaining wait time if still on cooldown."""
    pool = _get_pool()
    cooldown = PACK_COOLDOWNS[booster_type]
    async with pool.acquire() as conn:
        async with conn.transaction():
            last = await conn.fetchval(
                "SELECT last_opened_at FROM pack_cooldowns WHERE guild_id = $1 AND user_id = $2 AND booster_type = $3 FOR UPDATE",
                guild_id,
                user_id,
                booster_type,
            )
            now = datetime.now(timezone.utc)
            if last is not None:
                remaining = cooldown - (now - last)
                if remaining > timedelta(0):
                    return remaining
            await conn.execute(
                """
                INSERT INTO pack_cooldowns (guild_id, user_id, booster_type, last_opened_at)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (guild_id, user_id, booster_type) DO UPDATE SET last_opened_at = EXCLUDED.last_opened_at
                """,
                guild_id,
                user_id,
                booster_type,
                now,
            )
            return None


# ---------------------------------------------------------------------------
# Trading
# ---------------------------------------------------------------------------


async def create_trade(guild_id: int, from_user: int, to_user: int, offer_card_id: int, request_card_id: int) -> int:
    pool = _get_pool()
    return await pool.fetchval(
        """
        INSERT INTO trades (guild_id, from_user, to_user, offer_card_id, request_card_id)
        VALUES ($1, $2, $3, $4, $5) RETURNING id
        """,
        guild_id,
        from_user,
        to_user,
        offer_card_id,
        request_card_id,
    )


async def get_pending_trades(guild_id: int, user_id: int) -> list[asyncpg.Record]:
    pool = _get_pool()
    return await pool.fetch(
        "SELECT * FROM trades WHERE guild_id = $1 AND (from_user = $2 OR to_user = $2) AND status = 'pending' ORDER BY created_at DESC",
        guild_id,
        user_id,
    )


async def decline_trade(trade_id: int) -> None:
    pool = _get_pool()
    await pool.execute(
        "UPDATE trades SET status = 'declined', resolved_at = now() WHERE id = $1 AND status = 'pending'",
        trade_id,
    )


async def _move_card(conn: asyncpg.Connection, guild_id: int, from_user: int, to_user: int, card_id: int) -> None:
    await conn.execute(
        "UPDATE collection SET quantity = quantity - 1 WHERE guild_id = $1 AND user_id = $2 AND card_id = $3",
        guild_id,
        from_user,
        card_id,
    )
    await conn.execute(
        "DELETE FROM collection WHERE guild_id = $1 AND user_id = $2 AND card_id = $3 AND quantity <= 0",
        guild_id,
        from_user,
        card_id,
    )
    await conn.execute(
        """
        INSERT INTO collection (guild_id, user_id, card_id, quantity) VALUES ($1, $2, $3, 1)
        ON CONFLICT (guild_id, user_id, card_id) DO UPDATE SET quantity = collection.quantity + 1
        """,
        guild_id,
        to_user,
        card_id,
    )


async def execute_trade(trade_id: int) -> tuple[bool, str]:
    """Validate and atomically execute a pending trade."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            trade = await conn.fetchrow("SELECT * FROM trades WHERE id = $1 FOR UPDATE", trade_id)
            if trade is None or trade["status"] != "pending":
                return False, "This trade is no longer pending."

            guild_id = trade["guild_id"]
            offer_qty = await conn.fetchval(
                "SELECT quantity FROM collection WHERE guild_id = $1 AND user_id = $2 AND card_id = $3 FOR UPDATE",
                guild_id,
                trade["from_user"],
                trade["offer_card_id"],
            )
            request_qty = await conn.fetchval(
                "SELECT quantity FROM collection WHERE guild_id = $1 AND user_id = $2 AND card_id = $3 FOR UPDATE",
                guild_id,
                trade["to_user"],
                trade["request_card_id"],
            )

            if not offer_qty:
                await conn.execute(
                    "UPDATE trades SET status = 'cancelled', resolved_at = now() WHERE id = $1", trade_id
                )
                return False, "The offering side no longer owns their card."
            if not request_qty:
                await conn.execute(
                    "UPDATE trades SET status = 'cancelled', resolved_at = now() WHERE id = $1", trade_id
                )
                return False, "You no longer own the requested card."

            await _move_card(conn, guild_id, trade["from_user"], trade["to_user"], trade["offer_card_id"])
            await _move_card(conn, guild_id, trade["to_user"], trade["from_user"], trade["request_card_id"])

            await conn.execute(
                "UPDATE trades SET status = 'accepted', resolved_at = now() WHERE id = $1", trade_id
            )
            return True, "Trade completed!"
