"""The /tradeup slash command — thin Discord-facing wrapper around the
trade-up rules in utils/tradeup.py."""

import discord
from discord import app_commands
from discord.ext import commands

from utils import database, sets, tradeup


class TradeUp(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="tradeup",
        description=f"Spend {tradeup.TRADE_UP_COST} duplicates of one rarity for a random card of the next rarity up.",
    )
    @app_commands.describe(set_query="Set to trade up within", rarity="Rarity to spend duplicates of")
    @app_commands.rename(set_query="set")
    @app_commands.choices(
        # Only the three rarities that *can* be traded up from are offered
        # as choices — mythic has no rarity above it, so it's excluded here
        # rather than accepted and rejected later in utils.tradeup.trade_up.
        rarity=[
            app_commands.Choice(name="Common → Uncommon", value="common"),
            app_commands.Choice(name="Uncommon → Rare", value="uncommon"),
            app_commands.Choice(name="Rare → Mythic", value="rare"),
        ]
    )
    @app_commands.guild_only()
    async def tradeup_cmd(
        self, interaction: discord.Interaction, set_query: str, rarity: app_commands.Choice[str]
    ) -> None:
        """Resolve the requested set, verify it has cards cached, then hand
        off to utils.tradeup.trade_up to do the actual spend-and-draw. Any
        failure (ambiguous/unknown set, not enough duplicates, etc.) is
        reported back to the user as plain text rather than raised further.
        """
        await interaction.response.defer()

        try:
            set_row = await sets.resolve_set(set_query)
        except (sets.AmbiguousSetError, sets.SetNotFoundError) as e:
            await interaction.followup.send(str(e))
            return
        set_code, set_name = set_row["code"], set_row["name"]

        if not await database.set_is_cached(set_code):
            await interaction.followup.send(f"No cards cached for {set_name} ({set_code}).")
            return

        try:
            card = await tradeup.trade_up(interaction.guild_id, interaction.user.id, set_code, rarity.value)
        except tradeup.TradeUpError as e:
            await interaction.followup.send(str(e))
            return

        await interaction.followup.send(f"Traded up to **{card.name}** ({card.rarity.capitalize()})!")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TradeUp(bot))
