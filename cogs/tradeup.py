"""The /tradeup slash command — a private menu over the trade-up rules in
utils/tradeup.py. Pick a set (only sets with a trade-up you can afford,
most first, paged 25 at a time), then a rarity, then confirm. The card you
get is posted publicly, like a pack opening, and the menu refreshes so you
can keep going."""

import discord
from discord import app_commands
from discord.ext import commands

from utils import packs, sets, tradeup
from utils.packs import RARITY_EMOJI, card_line
from utils.tradeup import NEXT_RARITY, TRADE_UP_COST, SetSpares
from utils.views import SafeView

TRADEUP_VIEW_TIMEOUT_SECONDS = 300
SETS_PER_PAGE = 25  # Discord's cap on options in one select menu
RARITY_LETTER = {"common": "C", "uncommon": "U", "rare": "R"}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _spare_summary(entry: SetSpares) -> str:
    """e.g. "12 C · 6 U · 2 R spare"."""
    parts = [f"{entry.spare[r]} {RARITY_LETTER[r]}" for r in NEXT_RARITY if entry.spare.get(r)]
    return " · ".join(parts) + " spare"


class TradeUpView(SafeView):
    """The whole /tradeup menu, as one message that redraws itself. Three
    screens: the set picker (selected is None), a set's rarity buttons
    (pending is None), and the confirm step for one rarity."""

    def __init__(
        self, user_id: int, guild_id: int, spares: dict[str, SetSpares], selected: SetSpares | None = None
    ):
        super().__init__(timeout=TRADEUP_VIEW_TIMEOUT_SECONDS)
        self.user_id = user_id
        self.guild_id = guild_id
        self.page = 0
        self.selected = selected
        self.pending: str | None = None  # rarity awaiting confirmation
        self._set_spares(spares)
        self.render()

    def _set_spares(self, spares: dict[str, SetSpares]) -> None:
        self.spares = spares
        self.sets = tradeup.affordable_sets(spares)
        self.page = min(self.page, max(self.page_count - 1, 0))
        if self.selected is not None:
            # A set with no duplicates left drops out of `spares` — keep
            # showing it, with zero spares but the same possible trade-ups.
            old = self.selected
            self.selected = spares.get(old.set_code) or SetSpares(old.set_code, old.set_name, possible=old.possible)

    @property
    def page_count(self) -> int:
        return -(-len(self.sets) // SETS_PER_PAGE)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your trade-up.", ephemeral=True)
            return False
        return True

    # --- what's shown ------------------------------------------------------

    def embed(self) -> discord.Embed:
        if self.selected is None:
            return self._picker_embed()
        if self.pending is None:
            return self._set_embed()
        return self._confirm_embed()

    def _picker_embed(self) -> discord.Embed:
        embed = discord.Embed(title="Trade up", color=discord.Color.blurple())
        if not self.sets:
            embed.description = f"You don't have {TRADE_UP_COST} spare duplicates of one rarity in any set right now."
            return embed
        embed.description = (
            f"Spend **{TRADE_UP_COST}** duplicates of one rarity for a random card of the next rarity up.\n"
            "Pick a set — the ones with the most trade-ups are at the top."
        )
        footer = _plural(len(self.sets), "set")
        if self.page_count > 1:
            footer = f"Page {self.page + 1}/{self.page_count} · {footer}"
        embed.set_footer(text=footer)
        return embed

    def _set_embed(self) -> discord.Embed:
        entry = self.selected
        lines = []
        for rarity, next_rarity in NEXT_RARITY.items():
            spare = entry.spare.get(rarity, 0)
            line = f"{RARITY_EMOJI[rarity]} {rarity.capitalize()}: {spare} spare"
            if rarity not in entry.possible:
                line += f" — this set has no {next_rarity}s"
            elif spare >= TRADE_UP_COST:
                line += f" — {_plural(spare // TRADE_UP_COST, 'trade-up')}"
            lines.append(line)
        embed = discord.Embed(
            title=f"{entry.set_name} ({entry.set_code.upper()})",
            description=f"Spend **{TRADE_UP_COST}** duplicates for a random card of the next rarity up.\n\n"
            + "\n".join(lines),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Basic lands don't count. Alternate-art copies are spent last.")
        return embed

    def _confirm_embed(self) -> discord.Embed:
        entry, rarity = self.selected, self.pending
        return discord.Embed(
            title="Confirm trade-up",
            description=f"Spend **{TRADE_UP_COST} duplicate {rarity}s** from {entry.set_name} "
            f"for a random **{NEXT_RARITY[rarity]}**?\nThis can't be undone.",
            color=discord.Color.orange(),
        )

    def render(self) -> None:
        """Rebuild the components for the current screen."""
        self.clear_items()
        if self.selected is None:
            self._render_picker()
        elif self.pending is None:
            self._render_set()
        else:
            self._render_confirm()

    def _button(self, label: str, callback, *, style=discord.ButtonStyle.secondary, disabled=False, row=None) -> None:
        button = discord.ui.Button(label=label, style=style, disabled=disabled, row=row)
        button.callback = callback
        self.add_item(button)

    def _render_picker(self) -> None:
        if not self.sets:
            return
        start = self.page * SETS_PER_PAGE
        select = discord.ui.Select(
            placeholder="Pick a set",
            options=[
                discord.SelectOption(
                    label=entry.set_name[:100],
                    value=entry.set_code,
                    description=f"{_plural(entry.trade_ups, 'trade-up')} · {_spare_summary(entry)}"[:100],
                )
                for entry in self.sets[start : start + SETS_PER_PAGE]
            ],
            row=0,
        )
        select.callback = self._on_pick_set
        self.add_item(select)
        if self.page_count > 1:
            self._button("◀", self._on_prev_page, disabled=self.page == 0, row=1)
            self._button("▶", self._on_next_page, disabled=self.page >= self.page_count - 1, row=1)

    def _render_set(self) -> None:
        entry = self.selected
        for rarity, next_rarity in NEXT_RARITY.items():
            spare = entry.spare.get(rarity, 0)
            if rarity not in entry.possible:
                label = f"{rarity.capitalize()} → {next_rarity.capitalize()} (none in set)"
            else:
                label = f"{rarity.capitalize()} → {next_rarity.capitalize()} ({spare}/{TRADE_UP_COST})"
            self._button(
                label,
                self._choose_rarity(rarity),
                style=discord.ButtonStyle.primary,
                disabled=not entry.can_trade_up(rarity),
                row=0,
            )
        if self.sets:
            self._button("◀ All sets", self._on_back, row=1)

    def _render_confirm(self) -> None:
        self._button("Confirm", self._on_confirm, style=discord.ButtonStyle.success)
        self._button("Cancel", self._on_cancel)

    async def _redraw(self, interaction: discord.Interaction) -> None:
        self.render()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    # --- callbacks -----------------------------------------------------------

    async def _on_pick_set(self, interaction: discord.Interaction) -> None:
        code = interaction.data["values"][0]
        self.selected = self.spares[code]
        await self._redraw(interaction)

    async def _on_prev_page(self, interaction: discord.Interaction) -> None:
        self.page = max(self.page - 1, 0)
        await self._redraw(interaction)

    async def _on_next_page(self, interaction: discord.Interaction) -> None:
        self.page = min(self.page + 1, self.page_count - 1)
        await self._redraw(interaction)

    async def _on_back(self, interaction: discord.Interaction) -> None:
        self.selected = None
        await self._redraw(interaction)

    def _choose_rarity(self, rarity: str):
        async def callback(interaction: discord.Interaction) -> None:
            self.pending = rarity
            await self._redraw(interaction)

        return callback

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        self.pending = None
        await self._redraw(interaction)

    async def _on_confirm(self, interaction: discord.Interaction) -> None:
        entry, rarity = self.selected, self.pending
        # Grey everything out while the trade-up runs, so a double click
        # can't fire it twice.
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)

        error = None
        try:
            card = await tradeup.trade_up(self.guild_id, self.user_id, entry.set_code, rarity)
        except tradeup.TradeUpError as e:
            error = str(e)

        # Back to the set's screen with fresh counts, ready for another.
        self.pending = None
        self._set_spares(await tradeup.get_spares(self.guild_id, self.user_id))
        self.render()
        await interaction.edit_original_response(embed=self.embed(), view=self)

        if error:
            await interaction.followup.send(error, ephemeral=True)
            return
        result = discord.Embed(
            title=card_line(card),
            description=f"{interaction.user.display_name} traded up {TRADE_UP_COST} {rarity}s from {entry.set_name}.",
            color=discord.Color.blurple(),
        )
        if card.image_url:
            result.set_image(url=card.image_url)
        await interaction.followup.send(embed=result)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True


class TradeUp(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="tradeup",
        description=f"Spend {TRADE_UP_COST} duplicates of one rarity for a random card of the next rarity up.",
    )
    @app_commands.describe(set_query="Jump straight to one set — omit to pick from the sets you can trade up in")
    @app_commands.rename(set_query="set")
    @app_commands.guild_only()
    async def tradeup_cmd(self, interaction: discord.Interaction, set_query: str | None = None) -> None:
        """Open the private trade-up menu: the set picker, or with `set_query`
        that one set's rarity screen directly."""
        await interaction.response.defer(ephemeral=True)

        selected = None
        if set_query:
            try:
                set_row = await sets.resolve_set(set_query)
            except (sets.AmbiguousSetError, sets.SetNotFoundError) as e:
                await interaction.followup.send(str(e), ephemeral=True)
                return

        spares = await tradeup.get_spares(interaction.guild_id, interaction.user.id)
        if set_query:
            code = set_row["code"]
            selected = spares.get(code)
            if selected is None:  # no duplicates there at all — still show which trade-ups the set has
                available = await packs.available_rarities(code)
                possible = {rarity for rarity, next_rarity in NEXT_RARITY.items() if next_rarity in available}
                selected = SetSpares(code, set_row["name"], possible=possible)
        elif not tradeup.affordable_sets(spares):
            await interaction.followup.send(
                f"You don't have {TRADE_UP_COST} spare duplicates of one rarity in any set yet. "
                "Duplicates are copies beyond your first; basic lands don't count.",
                ephemeral=True,
            )
            return

        view = TradeUpView(interaction.user.id, interaction.guild_id, spares, selected)
        await interaction.followup.send(embed=view.embed(), view=view, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TradeUp(bot))
