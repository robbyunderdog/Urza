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

    available = await database.count_spendable_duplicates(guild_id, user_id, set_code, rarity)
    if available < TRADE_UP_COST:
        raise TradeUpError(
            f"You need {TRADE_UP_COST} duplicate {rarity}s from {set_code.upper()} to trade up, "
            f"you have {available}."
        )

    spent = await database.spend_duplicates(guild_id, user_id, set_code, rarity, TRADE_UP_COST)
    if spent < TRADE_UP_COST:
        raise TradeUpError("Someone spent your duplicates out from under you — try again.")

    rows = await database.get_random_cards(set_code, next_rarity, 1)
    row = rows[0]
    card = packs.PulledCard(str(row["id"]), row["name"], row["rarity"], row["image_url"])
    await database.add_to_collection(guild_id, user_id, [card.card_id])
    return card
