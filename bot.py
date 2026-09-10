import logging

import discord
from discord.ext import commands

import config
from utils.database import close_db, init_db

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("urza")

EXTENSIONS = ["cogs.general", "cogs.packs", "cogs.tradeup", "cogs.collection"]


class Urza(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)

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

    async def close(self) -> None:
        await close_db()
        await super().close()


def main() -> None:
    bot = Urza()
    bot.run(config.DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
