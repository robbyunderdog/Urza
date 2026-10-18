"""Trade-up rules: spend duplicate copies of one rarity for a random card
of the next rarity up, within a single set. Used by /tradeup.

Only copies beyond the first count as duplicates, and basic lands never
count (every pack has one, so they'd make commons free). A trade-up is
only offered when the set actually has cards of the next rarity — sets
printed before 2008 have no mythics, for one."""

from dataclasses import dataclass, field

from utils import database, packs

NEXT_RARITY = {"common": "uncommon", "uncommon": "rare", "rare": "mythic"}
TRADE_UP_COST = 10  # duplicate copies required to trade up one rarity tier


class TradeUpError(Exception):
    """Raised for any user-facing trade-up failure (invalid rarity, not
    enough duplicates, nothing at the destination rarity). The exception's
    message is shown to the user as-is."""

    pass


@dataclass
class SetSpares:
    """One set's spare duplicates per rarity, and which rarities it can
    trade up from (the next rarity exists in the set)."""

    set_code: str
    set_name: str
    spare: dict[str, int] = field(default_factory=dict)
    possible: set[str] = field(default_factory=set)

    def can_trade_up(self, rarity: str) -> bool:
        return rarity in self.possible and self.spare.get(rarity, 0) >= TRADE_UP_COST

    @property
    def trade_ups(self) -> int:
        """How many trade-ups this set can afford right now, across rarities."""
        return sum(self.spare.get(rarity, 0) // TRADE_UP_COST for rarity in self.possible)


async def get_spares(guild_id: int, user_id: int) -> dict[str, SetSpares]:
    """Every set this user has spare duplicates in, keyed by set code."""
    result: dict[str, SetSpares] = {}
    for row in await database.get_tradeup_spares(guild_id, user_id, NEXT_RARITY):
        entry = result.setdefault(row["set_code"], SetSpares(row["set_code"], row["set_name"]))
        if row["spare"]:
            entry.spare[row["rarity"]] = row["spare"]
        if row["possible"]:
            entry.possible.add(row["rarity"])
    return result


def affordable_sets(spares: dict[str, SetSpares]) -> list[SetSpares]:
    """Sets with at least one trade-up available, most trade-ups first."""
    eligible = [entry for entry in spares.values() if entry.trade_ups > 0]
    return sorted(eligible, key=lambda entry: (-entry.trade_ups, entry.set_name))


async def trade_up(guild_id: int, user_id: int, set_code: str, rarity: str) -> packs.PulledCard:
    """Spend `TRADE_UP_COST` duplicate copies of `rarity` from `set_code`
    for one random card of the next rarity up, and add it to the user's
    collection.

    Raises TradeUpError if `rarity` is already the top rarity (mythic), if
    the set has no cards at the destination rarity, or if the user doesn't
    have enough spendable duplicates.
    """
    if rarity not in NEXT_RARITY:
        raise TradeUpError(f"Can't trade up from {rarity} — there's nothing above it.")
    next_rarity = NEXT_RARITY[rarity]

    # Draw the card first: it doubles as the check that the destination
    # pool exists, before anything is spent.
    row = await database.get_tradeup_card(set_code, next_rarity)
    if row is None:
        raise TradeUpError(f"{set_code.upper()} has no {next_rarity}s to trade up into.")

    # All-or-nothing: spends exactly TRADE_UP_COST and awards the card in
    # one transaction, or touches nothing.
    if not await database.trade_up_duplicates(guild_id, user_id, set_code, rarity, TRADE_UP_COST, row["id"]):
        available = await database.count_spendable_duplicates(guild_id, user_id, set_code, rarity)
        raise TradeUpError(
            f"You need {TRADE_UP_COST} duplicate {rarity}s from {set_code.upper()} to trade up, "
            f"you have {available}."
        )

    return packs.PulledCard(
        row["id"], row["name"], row["rarity"], row["image_url"], special=row["is_special"], set_code=set_code
    )
