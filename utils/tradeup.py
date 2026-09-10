from utils import database, packs

RARITY_ORDER = ["common", "uncommon", "rare", "mythic"]
TRADE_UP_COST = 20


class TradeUpError(Exception):
    pass


async def trade_up(guild_id: int, user_id: int, set_code: str, rarity: str) -> packs.PulledCard:
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

    rows = await database.get_random_cards(set_code, next_rarity, 1)
    row = rows[0]
    card = packs.PulledCard(row["id"], row["name"], row["rarity"], row["image_url"])
    await database.add_to_collection(guild_id, user_id, [card.card_id])
    return card
