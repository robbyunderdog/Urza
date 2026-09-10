"""The /collection and /missing slash commands: everything about viewing a
user's per-server collection progress, either as an overall summary across
every cached set or a detailed breakdown/listing for one set."""

import discord
from discord import app_commands
from discord.ext import commands

from utils import database, sets
from utils.packs import RARITY_EMOJI
from utils.views import SafeView

SETS_PER_PAGE = 15
MAX_MISSING_SHOWN = 40
PAGINATOR_TIMEOUT_SECONDS = 300
RARITY_ORDER = ["common", "uncommon", "rare", "mythic"]

# Width (in monospace block characters) of every rendered progress bar. 24
# fills most of a standard Discord embed's width without wrapping onto a
# second line even next to a 3-4 digit "owned/total" count.
BAR_WIDTH = 24


def _progress_bar(owned: int, total: int, width: int = BAR_WIDTH) -> str:
    """Render an owned/total ratio as a block-character bar, e.g.
    "████████░░░░░░░░░░░░░░░░" for 8/24. Returns an em dash instead of a
    (meaningless) empty bar when `total` is 0."""
    if total == 0:
        return "—"
    filled = round(width * owned / total)
    return "█" * filled + "░" * (width - filled)


def _format_set_block(row, count_width: int) -> str:
    """One set's entry in the all-sets progress list: its name/code on one
    line, then a progress bar with the owned/total count on the next.

    `count_width` is passed in (computed once per page by the caller) so
    every bar on the same page right-aligns its count to the same column,
    regardless of whether that particular set's counts are e.g. "3/50" or
    "312/350" — without it, shorter counts would leave the bar+count line
    ragged instead of forming a clean column.
    """
    bar = _progress_bar(row["owned"], row["total"])
    name = row["name"] or row["code"].upper()
    counts = f"{row['owned']}/{row['total']}".rjust(count_width)
    # Bar line is wrapped in backticks (monospace) so every bar starts at the
    # same column and lines up cleanly regardless of how long the set name
    # above it is — the name itself stays in the embed's normal proportional
    # font.
    return f"**{name}** (`{row['code']}`)\n`{bar} {counts}`"


async def _build_set_rarity_embed(guild_id: int, user_id: int, display_name: str, set_code: str, set_name: str):
    """Per-rarity breakdown for one set — shows exactly which rarities have
    missing cards, unlike the all-sets list which only shows an overall bar."""
    progress = await database.get_set_progress(guild_id, user_id, set_code)
    embed = discord.Embed(title=f"{display_name}'s {set_name} Progress", color=discord.Color.green())
    total_owned = total_all = 0
    for rarity in RARITY_ORDER:
        owned, total = progress.get(rarity, (0, 0))
        total_owned += owned
        total_all += total
        embed.add_field(
            name=f"{RARITY_EMOJI.get(rarity, '')} {rarity.capitalize()}",
            value=f"`{_progress_bar(owned, total)} {owned}/{total}`",
            inline=False,
        )
    embed.set_footer(text=f"{total_owned}/{total_all} printings • /missing set:{set_code} to see exactly which cards")
    return embed, total_all


async def resolve_set_or_report(interaction: discord.Interaction, query: str):
    """Resolve free-text set input for /collection, reporting a friendly
    ephemeral error and returning None if it fails. Caller should return
    immediately when this returns None."""
    try:
        return await sets.resolve_set(query)
    except (sets.AmbiguousSetError, sets.SetNotFoundError) as e:
        await interaction.followup.send(str(e), ephemeral=True)
        return None


class SetListPaginator(SafeView):
    """Pages through every cached set's overall progress, newest first."""

    def __init__(self, user_id: int, display_name: str, rows: list):
        super().__init__(timeout=PAGINATOR_TIMEOUT_SECONDS)
        self.user_id = user_id  # only this user may click ◀/▶ — see interaction_check
        self.display_name = display_name
        # Split the flat row list into fixed-size pages up front, so paging
        # is just indexing into self.pages rather than re-slicing each time.
        # `or [[]]` guarantees at least one (empty) page exists even if
        # `rows` itself is empty, so current_embed never indexes an empty list.
        self.pages = [rows[i : i + SETS_PER_PAGE] for i in range(0, len(rows), SETS_PER_PAGE)] or [[]]
        self.index = 0

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Restrict paging buttons to the user whose collection this is."""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your collection view.", ephemeral=True)
            return False
        return True

    def current_embed(self) -> discord.Embed:
        """Render the page at `self.index` as an embed. Called for the
        initial message and again after every ◀/▶ click."""
        page = self.pages[self.index]
        count_width = max((len(f"{r['owned']}/{r['total']}") for r in page), default=0)
        blocks = [_format_set_block(row, count_width) for row in page]
        embed = discord.Embed(
            title=f"{self.display_name}'s Collection Progress",
            description="\n\n".join(blocks) or "No sets cached yet.",
            color=discord.Color.green(),
        )
        embed.set_footer(text=f"Page {self.index + 1}/{len(self.pages)} • newest sets first")
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Go back one page, wrapping from the first page to the last."""
        self.index = (self.index - 1) % len(self.pages)
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Go forward one page, wrapping from the last page back to the first."""
        self.index = (self.index + 1) % len(self.pages)
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    async def on_timeout(self) -> None:
        """Disable paging once PAGINATOR_TIMEOUT_SECONDS has elapsed with no
        interaction, so a stale view doesn't accept clicks forever."""
        for child in self.children:
            child.disabled = True


class Collection(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="collection", description="View your Magic collection: one set's rarity breakdown, or overall progress."
    )
    @app_commands.describe(set_query="Show this set's missing rarities instead of your overall progress")
    @app_commands.rename(set_query="set")
    @app_commands.guild_only()
    async def collection(self, interaction: discord.Interaction, set_query: str | None = None) -> None:
        """With `set_query`: show a per-rarity progress breakdown for that
        one set. With no argument: show a paginated overview of every
        cached set's overall progress, newest release first."""
        await interaction.response.defer()

        if set_query:
            set_row = await resolve_set_or_report(interaction, set_query)
            if set_row is None:
                return
            set_code, set_name = set_row["code"], set_row["name"]
            embed, total_all = await _build_set_rarity_embed(
                interaction.guild_id, interaction.user.id, interaction.user.display_name, set_code, set_name
            )
            if total_all == 0:
                await interaction.followup.send(f"No cards cached for {set_name} ({set_code}).")
                return
            await interaction.followup.send(embed=embed)
            return

        rows = await database.get_all_sets_progress(interaction.guild_id, interaction.user.id)
        if not rows:
            await interaction.followup.send("No sets cached yet.")
            return

        view = SetListPaginator(interaction.user.id, interaction.user.display_name, rows)
        await interaction.followup.send(embed=view.current_embed(), view=view)

    @app_commands.command(name="missing", description="List cards you don't own yet from a set, in this server.")
    @app_commands.describe(set_query="Set to check (omit for a totals summary across all cached sets)", rarity="Only show this rarity")
    @app_commands.rename(set_query="set")
    @app_commands.choices(
        rarity=[app_commands.Choice(name=r.capitalize(), value=r) for r in ["common", "uncommon", "rare", "mythic"]]
    )
    @app_commands.guild_only()
    async def missing(
        self, interaction: discord.Interaction, set_query: str | None = None, rarity: app_commands.Choice[str] | None = None
    ) -> None:
        """With no `set_query`: report the total number of missing cards
        and overall completion percentage across every cached set (rarity
        filtering doesn't apply here since there's no single set to filter
        within). With `set_query`: list every missing card in that set,
        optionally narrowed to one rarity.
        """
        await interaction.response.defer(ephemeral=True)

        if not set_query:
            # Reuses the same per-set (owned, total) rows /collection's
            # overview page is built from and sums them, rather than adding
            # a separate aggregate query — the totals are cheap to derive
            # from data already fetched one row per cached set.
            rows = await database.get_all_sets_progress(interaction.guild_id, interaction.user.id)
            if not rows:
                await interaction.followup.send("No sets cached yet.")
                return
            total = sum(row["total"] for row in rows)
            owned = sum(row["owned"] for row in rows)
            missing_count = total - owned
            percent = (owned / total * 100) if total else 0.0
            if missing_count == 0:
                await interaction.followup.send(f"Nothing missing across {len(rows)} cached sets — you have it all! (100% complete)")
                return
            embed = discord.Embed(
                title="Missing Cards — All Sets",
                description=(
                    f"You're missing **{missing_count}** card{'s' if missing_count != 1 else ''} "
                    f"across {len(rows)} cached sets.\nOverall completion: **{percent:.1f}%** ({owned}/{total})"
                ),
                color=discord.Color.red(),
            )
            await interaction.followup.send(embed=embed)
            return

        try:
            set_row = await sets.resolve_set(set_query)
        except (sets.AmbiguousSetError, sets.SetNotFoundError) as e:
            await interaction.followup.send(str(e))
            return
        set_code, set_name = set_row["code"], set_row["name"]

        if not await database.set_is_cached(set_code):
            await interaction.followup.send(f"No cards cached for {set_name} ({set_code}).")
            return

        rows = await database.get_missing_cards(
            interaction.guild_id, interaction.user.id, set_code, rarity.value if rarity else None
        )
        if not rows:
            await interaction.followup.send("Nothing missing — you have it all!")
            return

        names = [f"{RARITY_EMOJI.get(row['rarity'], '')} {row['name']} (#{row['collector_number']})" for row in rows]
        shown = names[:MAX_MISSING_SHOWN]
        text = "\n".join(shown)
        if len(names) > MAX_MISSING_SHOWN:
            text += f"\n...and {len(names) - MAX_MISSING_SHOWN} more"

        embed = discord.Embed(title=f"Missing from {set_name}", description=text, color=discord.Color.red())
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Collection(bot))
