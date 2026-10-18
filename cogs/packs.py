"""The /rip slash command: claim a user's free periodic pack (subject to a
per-server cooldown), generate the pack via utils/packs.py, record it into
their collection, and present the result as one grid image of the whole
pack (see utils/pack_image.py), with an optional card-by-card view.
"""

import asyncio
import io

import discord
from discord import app_commands
from discord.ext import commands

from utils import database, pack_image, packs, sets
from utils.packs import PACK_TYPE, card_line
from utils.views import SafeView

PACK_VIEW_TIMEOUT_SECONDS = 300


class PackRevealView(SafeView):
    """Flip through a freshly opened pack one card at a time — opened
    privately from PackGridView's "View cards" button."""

    def __init__(self, user_id: int, header: str, pulled: list[packs.PulledCard]):
        super().__init__(timeout=PACK_VIEW_TIMEOUT_SECONDS)
        self.user_id = user_id  # only this user may click the buttons — see interaction_check
        self.header = header
        self.pulled = pulled
        self.index = 0

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Restrict button clicks to the person who opened the pack — anyone
        else clicking gets a private "not yours" message instead of being
        able to flip through someone else's pull."""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your pack.", ephemeral=True)
            return False
        return True

    def current_embed(self) -> discord.Embed:
        """Render the card at `self.index` as the embed the view currently
        shows. Called both for the initial message and after every ◀/▶ click."""
        card = self.pulled[self.index]
        embed = discord.Embed(title=card_line(card), description=card.rarity.capitalize(), color=discord.Color.blurple())
        if card.image_url:
            embed.set_image(url=card.image_url)
        embed.set_author(name=self.header)
        embed.set_footer(text=f"Card {self.index + 1}/{len(self.pulled)}")
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Step back one card, wrapping from the first card to the last."""
        self.index = (self.index - 1) % len(self.pulled)
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Step forward one card, wrapping from the last card back to the first."""
        self.index = (self.index + 1) % len(self.pulled)
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    async def on_timeout(self) -> None:
        """Disable the buttons once PACK_VIEW_TIMEOUT_SECONDS has elapsed
        with no interaction, so a stale view doesn't accept clicks forever."""
        for child in self.children:
            child.disabled = True


class PackGridView(SafeView):
    """Sits under a whole-pack grid image: "View cards" opens the
    card-by-card PackRevealView privately, for a full-size look."""

    def __init__(self, user_id: int, header: str, pulled: list[packs.PulledCard]):
        super().__init__(timeout=PACK_VIEW_TIMEOUT_SECONDS)
        self.user_id = user_id
        self.header = header
        self.pulled = pulled

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your pack.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="View cards", style=discord.ButtonStyle.secondary)
    async def view_cards(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        view = PackRevealView(self.user_id, self.header, self.pulled)
        await interaction.response.send_message(embed=view.current_embed(), view=view, ephemeral=True)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True


async def send_pack(
    interaction: discord.Interaction, header: str, pulled: list[packs.PulledCard], image: bytes
) -> None:
    """Send an opened pack as a followup: the composited grid image of the
    whole pack (see pack_image.render_pack_grid) with the card list above
    it, and a "View cards" button for a full-size card-by-card look.
    `pulled` must already be in display order (packs.sort_by_rarity)."""
    embed = discord.Embed(description="\n".join(card_line(card) for card in pulled), color=discord.Color.blurple())
    embed.set_author(name=header)
    embed.set_image(url="attachment://pack.jpg")
    view = PackGridView(interaction.user.id, header, pulled)
    await interaction.followup.send(embed=embed, file=discord.File(io.BytesIO(image), "pack.jpg"), view=view)


def _format_remaining(remaining) -> str:
    """Render a cooldown timedelta as a short "Xh Ym" string for display."""
    total_seconds = int(remaining.total_seconds())
    hours, rem = divmod(total_seconds, 3600)
    minutes = rem // 60
    return f"{hours}h {minutes}m"


async def resolve_set_or_report(interaction: discord.Interaction, query: str):
    """Resolve free-text set input, reporting a friendly error and returning
    None if it fails. Caller should return immediately when this returns None.
    """
    try:
        return await sets.resolve_set(query)
    except (sets.AmbiguousSetError, sets.SetNotFoundError) as e:
        await interaction.followup.send(str(e))
        return None


async def resolve_set_or_random(interaction: discord.Interaction, set_query: str | None):
    """Resolve the set to open: the user's explicit `set_query` if given,
    otherwise a uniformly random pick among sets with cards already cached
    (see database.get_random_open_eligible_set). Reports a friendly error and
    returns None on failure; caller should return immediately in that case.
    """
    if set_query:
        return await resolve_set_or_report(interaction, set_query)

    set_row = await database.get_random_open_eligible_set()
    if set_row is None:
        await interaction.followup.send(
            "No sets are cached to open yet — ask an admin to run `python scripts/sync_sets.py` "
            "and `python scripts/sync_all_cards.py` first."
        )
        return None
    return set_row


class Packs(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="rip", description="Open your free periodic pack.")
    @app_commands.describe(set_query="Set name or code, e.g. mh3 or 'Modern Horizons 3' — omit for a random set")
    @app_commands.rename(set_query="set")
    @app_commands.guild_only()
    async def rip_cmd(self, interaction: discord.Interaction, set_query: str | None = None) -> None:
        """Order of checks, each of which can end the command early:
        1. A set must be resolvable: the explicit `set_query`, or else a
           random openable set (see resolve_set_or_random).
        2. The set must actually have cards cached in the database (via
           packs.available_rarities, which is cached in memory). (Also
           only reachable via an explicit `set_query` — a random pick is
           already guaranteed to be cached.)
        3. The user must not still be on cooldown.

        Only once all three pass does it actually generate the pack and
        record it into the user's collection.
        """
        await interaction.response.defer()

        set_row = await resolve_set_or_random(interaction, set_query)
        if set_row is None:
            return
        set_code, set_name = set_row["code"], set_row["name"]

        if not await packs.available_rarities(set_code):
            await interaction.followup.send(
                f"No cards cached for {set_name} ({set_code}) yet — ask an admin to run "
                f"`python scripts/sync_set.py {set_code}` first."
            )
            return

        remaining = await database.claim_free_pack(interaction.guild_id, interaction.user.id, PACK_TYPE)
        if remaining is not None:
            await interaction.followup.send(f"Your next free pack is ready in {_format_remaining(remaining)}.")
            return

        # The cooldown is already claimed at this point — if anything below
        # fails before the cards are recorded, give the claim back rather
        # than silently costing the user their free pack for a transient
        # failure. Rendering the image runs alongside the collection write,
        # since neither depends on the other.
        render = None
        try:
            pulled = packs.sort_by_rarity(await packs.generate_pack(set_code))
            render = asyncio.create_task(pack_image.render_pack_grid(pulled))
            await packs.record_pack(interaction.guild_id, interaction.user.id, pulled)
        except Exception:
            if render is not None:
                render.cancel()
            await database.release_pack_claim(interaction.guild_id, interaction.user.id, PACK_TYPE)
            raise

        header = f"{interaction.user.display_name}'s {set_name} pack"
        await send_pack(interaction, header, pulled, await render)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Packs(bot))
