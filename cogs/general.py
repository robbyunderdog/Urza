"""Miscellaneous commands not tied to the collection/pack feature set."""

import discord
from discord import app_commands
from discord.ext import commands


class General(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="ping", description="Check that Urza is alive.")
    async def ping(self, interaction: discord.Interaction) -> None:
        """Health check. Reports the bot's current websocket (gateway)
        latency, which is a decent proxy for "is the bot responsive"."""
        latency_ms = round(self.bot.latency * 1000)
        await interaction.response.send_message(f"Pong! ({latency_ms}ms)")


async def setup(bot: commands.Bot) -> None:
    """Entry point discord.py calls when this module is loaded as an
    extension (see EXTENSIONS in bot.py)."""
    await bot.add_cog(General(bot))
