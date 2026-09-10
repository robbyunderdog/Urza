"""Trade-up rules: spend duplicate copies of one rarity for a random card
of the next rarity up, within a single set. Used by /tradeup."""

from utils import database, packs

RARITY_ORDER = ["common", "uncommon", "rare", "mythic"]
TRADE_UP_COST = 20  # duplicate copies required to trade up one rarity tier


class TradeUpError(Exception):
    """Raised for any user-facing trade-up failure (invalid rarity, not
    enough duplicates, nothing cached at the destination rarity). The
    exception's message is shown to the user as-is."""

    pass


async def trade_up(guild_id: int, user_id: int, set_code: str, rarity: str) -> packs.PulledCard:
    """Spend `TRADE_UP_COST` duplicate copies of `rarity` from `set_code`
    for one random card of the next rarity up, and add it to the user's
    collection.

    Raises TradeUpError if `rarity` is already the top rarity (mythic), if
    the set has no cards cached at the destination rarity, or if the user
    doesn't have enough spendable duplicates.
    """
    if rarity not in RARITY_ORDER[:-1]:
        raise TradeUpError(f"Can't trade up from {rarity} — there's nothing above it.")

    next_rarity = RARITY_ORDER[RARITY_ORDER.index(rarity) + 1]

    # Check the destination pool exists before spending anything.
    if not await database.get_random_cards(set_code, next_rarity, 1):
        raise TradeUpError(f"No {next_rarity} cards cached for set {set_code.upper()}.")

    # spend_duplicates is all-or-nothing under one row lock, so there's no
    # race between checking availability and spending — either this takes
    # exactly TRADE_UP_COST or it takes nothing.
    spent = await database.spend_duplicates(guild_id, user_id, set_code, rarity, TRADE_UP_COST)
    if spent == 0:
        available = await database.count_spendable_duplicates(guild_id, user_id, set_code, rarity)
        raise TradeUpError(
            f"You need {TRADE_UP_COST} duplicate {rarity}s from {set_code.upper()} to trade up, "
            f"you have {available}."
        )

    # Drawn independently of the earlier existence check above — that check
    # only confirmed the pool was non-empty at the time; drawing again here
    # (post-spend) gets the actual card to award.
    rows = await database.get_random_cards(set_code, next_rarity, 1)
    row = rows[0]
    card = packs.PulledCard(row["id"], row["name"], row["rarity"], row["image_url"])
    await database.add_to_collection(guild_id, user_id, [card.card_id])
    return card
