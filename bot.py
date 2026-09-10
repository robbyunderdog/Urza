import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils.database import close_db, init_db

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("urza")

EXTENSIONS = ["cogs.general", "cogs.packs", "cogs.tradeup", "cogs.collection"]

ERROR_MESSAGE = "Something went wrong running that command. Try again in a moment."


async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    original = getattr(error, "original", error)
    command_name = interaction.command.qualified_name if interaction.command else "?"
    log.exception("Unhandled error in /%s", command_name, exc_info=original)

    try:
        if interaction.response.is_done():
            await interaction.followup.send(ERROR_MESSAGE, ephemeral=True)
        else:
            await interaction.response.send_message(ERROR_MESSAGE, ephemeral=True)
    except discord.HTTPException:
        pass  # interaction token may already be invalid/expired — nothing more we can do


class Urza(commands.AutoShardedBot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.tree.on_error = on_tree_error

    async def setup_hook(self) -> None:
        await init_db()

        for extension in EXTENSIONS:
            await self.load_extension(extension)

        if config.DEV_GUILD_ID:
            guild = discord.Object(id=int(config.DEV_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Synced commands to dev guild %s", config.DEV_GUILD_ID)
        else:
            await self.tree.sync()
            log.info("Synced commands globally")

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id)

    async def on_error(self, event_method: str, *args, **kwargs) -> None:
        log.exception("Unhandled error in event %s", event_method)

    async def close(self) -> None:
        await close_db()
        await super().close()


def main() -> None:
    bot = Urza()
    bot.run(config.DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
