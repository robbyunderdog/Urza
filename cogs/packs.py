"""The /ripbooster and /ripcollector slash commands: claim a user's free
periodic booster (subject to a per-server, per-booster-type cooldown),
generate the pack via utils/packs.py, record it into their collection, and
present the result as a one-card-at-a-time flip-through view.

The two commands are identical except for which booster type they open —
that's baked into the command itself (see Packs.ripbooster/ripcollector)
rather than being an input field, so opening a Collector Booster is just
`/ripcollector` instead of `/open booster_type:Collector Booster`.
"""

import discord
from discord import app_commands
from discord.ext import commands

from utils import database, packs, sets
from utils.packs import BOOSTER_LABELS, RARITY_EMOJI
from utils.views import SafeView

PACK_VIEW_TIMEOUT_SECONDS = 300


def _card_title(card: packs.PulledCard) -> str:
    """Rarity emoji + name, with cosmetic emoji suffixes for any special
    treatment this particular pull got (special-treatment printing, or a
    bonus pull from a linked Commander deck)."""
    title = f"{RARITY_EMOJI.get(card.rarity, '')} {card.name}"
    if card.special:
        title += " 🎨"
    if card.bonus:
        title += " 🎁"
    return title


class PackRevealView(SafeView):
    """Flip through a freshly opened pack one card at a time."""

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
        embed = discord.Embed(title=_card_title(card), description=card.rarity.capitalize(), color=discord.Color.blurple())
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


def _format_remaining(remaining) -> str:
    """Render a cooldown timedelta as a short "Xh Ym" string for display."""
    total_seconds = int(remaining.total_seconds())
    hours, rem = divmod(total_seconds, 3600)
    minutes = rem // 60
    return f"{hours}h {minutes}m"


async def resolve_set_or_report(interaction: discord.Interaction, query: str):
    """Resolve free-text set input, reporting a friendly error and returning
    None if it fails. Caller should return immediately when this returns None.

    Passes exclude_commander=True — a set-specific Commander deck (e.g.
    "Tarkir: Dragonstorm Commander") has no booster of its own (see the
    commander-set_type check further down in _rip), so if a user's query
    fuzzy-matches both a base set and its Commander deck, only the base set
    should be offered here rather than an ambiguous "did you mean" prompt
    between an openable and a non-openable set.
    """
    try:
        return await sets.resolve_set(query, exclude_commander=True)
    except (sets.AmbiguousSetError, sets.SetNotFoundError) as e:
        await interaction.followup.send(str(e))
        return None


async def resolve_set_or_random(interaction: discord.Interaction, set_query: str | None):
    """Resolve the set to open: the user's explicit `set_query` if given,
    otherwise a uniformly random pick among sets that are actually
    openable — not a Commander deck, and with cards already cached (see
    database.get_random_open_eligible_set). Reports a friendly error and
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

    async def _rip(self, interaction: discord.Interaction, set_query: str | None, booster_value: str) -> None:
        """Shared implementation behind /ripbooster and /ripcollector —
        only the booster type differs between the two commands.

        Order of checks, each of which can end the command early:
        1. A set must be resolvable: the explicit `set_query`, or else a
           random openable set (see resolve_set_or_random).
        2. The resolved set must not itself be a Commander deck — those
           redirect the user to open Collector Boosters of the base set.
           (Only reachable when `set_query` names one explicitly — a random
           pick already excludes Commander decks.)
        3. The set must actually have cards cached in the database. (Also
           only reachable via an explicit `set_query` — a random pick is
           already guaranteed to be cached.)
        4. The user must not still be on cooldown for this booster type.

        Only once all four pass does it actually generate the pack and
        record it into the user's collection.
        """
        await interaction.response.defer()

        set_row = await resolve_set_or_random(interaction, set_query)
        if set_row is None:
            return
        set_code, set_name = set_row["code"], set_row["name"]

        if set_row["set_type"] == "commander":
            # Reachable if the user typed the Commander deck's exact code or
            # name (resolve_set's exact-match checks aren't filtered by
            # exclude_commander) — explain why it can't be opened directly
            # and point them at the right command instead of just failing.
            parent = await database.get_set_by_code(set_row["parent_set_code"]) if set_row["parent_set_code"] else None
            parent_name = parent["name"] if parent else "its base set"
            await interaction.followup.send(
                f"{set_name} doesn't have its own booster — it's a Commander deck. Its cards show up as a rare "
                f"bonus pull in {parent_name}'s Collector Boosters instead (`/ripcollector set:{set_row['parent_set_code']}`)."
            )
            return

        if not await database.set_is_cached(set_code):
            await interaction.followup.send(
                f"No cards cached for {set_name} ({set_code}) yet — ask an admin to run "
                f"`python scripts/sync_set.py {set_code}` first."
            )
            return

        remaining = await database.claim_free_pack(interaction.guild_id, interaction.user.id, booster_value)
        if remaining is not None:
            await interaction.followup.send(
                f"Your next free {BOOSTER_LABELS[booster_value]} is ready in {_format_remaining(remaining)}."
            )
            return

        try:
            pulled = await packs.open_pack(interaction.guild_id, interaction.user.id, set_code, booster_value)
        except Exception:
            # The cooldown was already claimed above — if generating the pack
            # blew up partway through, give it back rather than silently
            # costing the user their free pack for a transient failure.
            await database.release_pack_claim(interaction.guild_id, interaction.user.id, booster_value)
            raise

        header = f"{interaction.user.display_name}'s {set_name} {BOOSTER_LABELS[booster_value]}"
        view = PackRevealView(interaction.user.id, header, pulled)
        await interaction.followup.send(embed=view.current_embed(), view=view)

    @app_commands.command(name="ripbooster", description="Open your free periodic Play Booster (every 4h).")
    @app_commands.describe(set_query="Set name or code, e.g. mh3 or 'Modern Horizons 3' — omit for a random set")
    @app_commands.rename(set_query="set")
    @app_commands.guild_only()
    async def ripbooster_cmd(self, interaction: discord.Interaction, set_query: str | None = None) -> None:
        await self._rip(interaction, set_query, "play")

    @app_commands.command(name="ripcollector", description="Open your free periodic Collector Booster (every 8h).")
    @app_commands.describe(set_query="Set name or code, e.g. mh3 or 'Modern Horizons 3' — omit for a random set")
    @app_commands.rename(set_query="set")
    @app_commands.guild_only()
    async def ripcollector_cmd(self, interaction: discord.Interaction, set_query: str | None = None) -> None:
        await self._rip(interaction, set_query, "collector")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Packs(bot))
