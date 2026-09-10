"""Entry point for the Urza Discord bot.

Running this module (`python bot.py`) does four things, in order:
1. Builds the `Urza` bot instance (a `commands.AutoShardedBot` subclass).
2. In `setup_hook`, initializes the Postgres connection pool and applies
   `db/schema.sql` (see `utils.database.init_db`).
3. Loads every cog listed in `EXTENSIONS` — each one registers its slash
   commands on `bot.tree`.
4. Syncs those slash commands with Discord: to a single guild instantly if
   `DEV_GUILD_ID` is set (handy while developing), otherwise globally
   (which can take up to an hour to propagate to every server).

`on_tree_error` is registered as the catch-all handler for any exception
raised inside a slash command, so an unexpected bug in a command surfaces a
friendly ephemeral message to the user instead of the interaction silently
timing out.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils.database import close_db, init_db

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("urza")

# Dotted import paths for each cog module — see cogs/*.py. Every module here
# must define an async `setup(bot)` function per discord.py's extension API.
EXTENSIONS = ["cogs.general", "cogs.packs", "cogs.tradeup", "cogs.collection"]

ERROR_MESSAGE = "Something went wrong running that command. Try again in a moment."


async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    """Global fallback for uncaught exceptions raised inside any slash
    command. Logs the full traceback for debugging, then tells the user
    something went wrong without leaking internal error details to them.

    Handles both cases of interaction state: if the command already sent a
    response (e.g. via `defer()`), a followup message is used instead of a
    fresh response, since Discord only allows one initial response per
    interaction.
    """
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
    """The bot itself. Auto-sharded so it scales past the ~2,500-guild
    single-shard limit without code changes if this ever grows that large."""

    def __init__(self) -> None:
        # `Intents.default()` is enough — every feature here is driven by
        # slash commands and button interactions, not message content or
        # member-list events, so no privileged intents need to be requested.
        intents = discord.Intents.default()
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.tree.on_error = on_tree_error

    async def setup_hook(self) -> None:
        """Called once by discord.py after login but before the gateway
        connection is fully established — the recommended place to do
        async setup work like connecting to a database or loading cogs."""
        await init_db()

        for extension in EXTENSIONS:
            await self.load_extension(extension)

        if config.DEV_GUILD_ID:
            # Copies every globally-registered command into this one guild
            # and syncs only there — Discord applies guild-scoped command
            # updates immediately, vs. up to an hour for global commands.
            # Ideal for iterating on command changes during development.
            guild = discord.Object(id=int(config.DEV_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Synced commands to dev guild %s", config.DEV_GUILD_ID)
        else:
            await self.tree.sync()
            log.info("Synced commands globally")

    async def on_ready(self) -> None:
        """Fired every time the gateway connection is (re)established.
        Purely informational — no setup should happen here since it can
        fire multiple times per process (e.g. after a reconnect)."""
        log.info("Logged in as %s (%s)", self.user, self.user.id)

    async def on_error(self, event_method: str, *args, **kwargs) -> None:
        """Catch-all for exceptions raised inside any discord.py event
        handler (on_message, on_member_join, etc.) that isn't a slash
        command — those go through on_tree_error instead. Without this
        override, discord.py's default behavior is to print to stderr and
        keep going; logging it via `log.exception` keeps it in the same
        place as everything else."""
        log.exception("Unhandled error in event %s", event_method)

    async def close(self) -> None:
        """Ensures the Postgres pool is closed cleanly on shutdown (Ctrl+C,
        SIGTERM, etc.) before delegating to discord.py's own cleanup."""
        await close_db()
        await super().close()


def main() -> None:
    bot = Urza()
    # log_handler=None: logging is already configured above via
    # logging.basicConfig, so discord.py's own handler setup is skipped to
    # avoid duplicate/conflicting log configuration.
    bot.run(config.DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
