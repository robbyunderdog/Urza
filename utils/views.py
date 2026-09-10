"""Shared discord.ui.View base class.

By default, an exception raised inside a button callback (like the ◀/▶
pagination buttons used by PackRevealView and SetListPaginator) is swallowed
by discord.py's default View.on_error, which just prints a traceback — the
user who clicked the button never finds out anything happened. Every view
with buttons in this project should subclass SafeView instead of
discord.ui.View directly to get proper error reporting for free.
"""

import logging

import discord

log = logging.getLogger("urza")

ERROR_MESSAGE = "Something went wrong with that. Try the command again."


class SafeView(discord.ui.View):
    """Base for views with buttons — logs and reports button-callback errors
    instead of letting them vanish into the console with no user feedback."""

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        """Overrides discord.ui.View's default (silent-to-the-user) error
        handling. Logs the full traceback, then tries to tell the clicking
        user something broke — using a followup if this interaction was
        already responded to (or deferred), otherwise a fresh response.
        """
        log.exception("Unhandled error in view %s, item %s", type(self).__name__, item, exc_info=error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(ERROR_MESSAGE, ephemeral=True)
            else:
                await interaction.response.send_message(ERROR_MESSAGE, ephemeral=True)
        except discord.HTTPException:
            pass  # interaction token may already be invalid/expired — nothing more we can do
