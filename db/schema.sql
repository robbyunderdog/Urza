-- Printing-level card cache, populated from the Scryfall API (every English
-- printing). Keyed by Scryfall's print id since the same card (oracle_id)
-- can have many printings with different art/set/collector number, and both
-- pack opening and collection tracking need to know exactly which printing
-- is involved.
CREATE TABLE IF NOT EXISTS cards (
    id UUID PRIMARY KEY,
    oracle_id UUID NOT NULL,
    name TEXT NOT NULL,
    set_code TEXT NOT NULL,
    collector_number TEXT NOT NULL,
    rarity TEXT NOT NULL,
    is_basic_land BOOLEAN NOT NULL DEFAULT FALSE,
    frame_effects TEXT[] NOT NULL DEFAULT '{}',
    finishes TEXT[] NOT NULL DEFAULT '{}',
    full_art BOOLEAN NOT NULL DEFAULT FALSE,
    border_color TEXT,
    mana_cost TEXT,
    type_line TEXT,
    image_url TEXT
);

-- Additive migrations for databases created before finishes/full_art/
-- border_color existed (CREATE TABLE IF NOT EXISTS above is a no-op on an
-- already-existing table).
ALTER TABLE cards ADD COLUMN IF NOT EXISTS finishes TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE cards ADD COLUMN IF NOT EXISTS full_art BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE cards ADD COLUMN IF NOT EXISTS border_color TEXT;

CREATE INDEX IF NOT EXISTS idx_cards_set_rarity ON cards (set_code, rarity);
CREATE INDEX IF NOT EXISTS idx_cards_set_land ON cards (set_code, is_basic_land);
CREATE INDEX IF NOT EXISTS idx_cards_oracle_id ON cards (oracle_id);
CREATE INDEX IF NOT EXISTS idx_cards_frame_effects ON cards USING GIN (frame_effects);

-- Set metadata, used to resolve free-text set names ("mh3", "Modern
-- Horizons 3") to a set code. See utils/sets.py. parent_set_code links a
-- set-specific Commander deck (set_type='commander') to its base expansion
-- (e.g. "Dominaria United Commander" -> "dmu") — those decks have no
-- booster of their own; their cards are a bonus pull from the base set's
-- Collector Boosters instead. The generic yearly "Commander 20XX" releases
-- have no parent_set_code and are excluded entirely (see utils/sets.py).
CREATE TABLE IF NOT EXISTS sets (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    set_type TEXT,
    released_at DATE
);

-- Additive migration for databases created before parent_set_code existed
-- (CREATE TABLE IF NOT EXISTS above is a no-op on an already-existing table).
ALTER TABLE sets ADD COLUMN IF NOT EXISTS parent_set_code TEXT;

-- A user's collection, tracked per-printing (card_id, not oracle_id — two
-- different printings of the same card are two different collectibles) and
-- per-server, so the same Discord user has an independent collection in
-- each server the bot is in.
CREATE TABLE IF NOT EXISTS collection (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    card_id UUID NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id, card_id)
);

CREATE INDEX IF NOT EXISTS idx_collection_guild_user ON collection (guild_id, user_id);

-- Per-server, per-user, per-booster-type free pack claim cooldown.
CREATE TABLE IF NOT EXISTS pack_cooldowns (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    booster_type TEXT NOT NULL,
    last_opened_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (guild_id, user_id, booster_type)
);

-- Player-to-player trade offers, scoped to the server they were made in
-- (since collections themselves are per-server).
CREATE TABLE IF NOT EXISTS trades (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    from_user BIGINT NOT NULL,
    to_user BIGINT NOT NULL,
    offer_card_id UUID NOT NULL,
    request_card_id UUID NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_trades_to_user_status ON trades (guild_id, to_user, status);
CREATE INDEX IF NOT EXISTS idx_trades_from_user_status ON trades (guild_id, from_user, status);
