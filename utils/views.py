import logging

import discord

log = logging.getLogger("urza")

ERROR_MESSAGE = "Something went wrong with that. Try the command again."


class SafeView(discord.ui.View):
    """Base for views with buttons — logs and reports button-callback errors
    instead of letting them vanish into the console with no user feedback."""

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        log.exception("Unhandled error in view %s, item %s", type(self).__name__, item, exc_info=error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(ERROR_MESSAGE, ephemeral=True)
            else:
                await interaction.response.send_message(ERROR_MESSAGE, ephemeral=True)
        except discord.HTTPException:
            pass
