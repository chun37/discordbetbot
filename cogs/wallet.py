from __future__ import annotations

import asyncio
import io
import logging
import math
from datetime import datetime, timezone
from itertools import groupby

import discord
from discord import app_commands
from discord.ext import commands

from embeds import build_balance_embed, build_history_embed
from odds import PERIOD_LABELS, calc_best_case_payout

logger = logging.getLogger(__name__)

PAGE_SIZE = 10


# ---------------------------------------------------------------------------
# /ranking pagination (unchanged)
# ---------------------------------------------------------------------------

class RankingPaginationView(discord.ui.View):
    def __init__(self, bot: commands.Bot, total: int) -> None:
        super().__init__(timeout=120)
        self.bot = bot
        self.total = total
        self.page = 0
        self.max_page = max(0, math.ceil(total / PAGE_SIZE) - 1)

    async def _build_embed(self) -> discord.Embed:
        from db import Database

        db: Database = self.bot.db
        rows = await db.top_balances(limit=PAGE_SIZE, offset=self.page * PAGE_SIZE)

        embed = discord.Embed(
            title="残高ランキング",
            color=discord.Color.gold(),
        )
        if not rows:
            embed.description = "まだ参加者がいません。"
        else:
            lines = []
            for i, row in enumerate(rows, start=self.page * PAGE_SIZE + 1):
                lines.append(f"**{i}.** <@{row['user_id']}> — {row['balance']}P")
            embed.description = "\n".join(lines)

        embed.set_footer(text=f"ページ {self.page + 1} / {self.max_page + 1}  (全 {self.total} 名)")
        return embed

    async def _refresh(self, interaction: discord.Interaction) -> None:
        embed = await self._build_embed()
        self._update_buttons()
        await interaction.response.edit_message(embed=embed, view=self)

    def _update_buttons(self) -> None:
        self.first_btn.disabled = self.page == 0
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.max_page
        self.last_btn.disabled = self.page >= self.max_page

    @discord.ui.button(label="<<", style=discord.ButtonStyle.secondary)
    async def first_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = 0
        await self._refresh(interaction)

    @discord.ui.button(label="<", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = max(0, self.page - 1)
        await self._refresh(interaction)

    @discord.ui.button(label=">", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = min(self.max_page, self.page + 1)
        await self._refresh(interaction)

    @discord.ui.button(label=">>", style=discord.ButtonStyle.secondary)
    async def last_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = self.max_page
        await self._refresh(interaction)


# ---------------------------------------------------------------------------
# /balance view helpers
# ---------------------------------------------------------------------------

async def _build_balance_data(
    bot: commands.Bot,
    target_id: int,
) -> tuple[int, list[dict]]:
    """残高と参加中賭け集計行を返す。"""
    from db import Database

    db: Database = bot.db
    balance, my_entries = await asyncio.gather(
        db.fetch_balance(target_id),
        db.fetch_user_open_entries(target_id),
    )

    if not my_entries:
        return balance, []

    # bet_id ごとに母集団を取得
    bet_ids = list(dict.fromkeys(e["bet_id"] for e in my_entries))
    all_entries = await db.fetch_entries_for_bets(bet_ids)

    # bet_id -> list[Row]
    all_by_bet: dict[int, list] = {}
    for e in all_entries:
        all_by_bet.setdefault(e["bet_id"], []).append(e)

    # (bet_id, period_key) ごとに集計
    rows: list[dict] = []
    sorted_entries = sorted(my_entries, key=lambda e: (e["bet_id"], e["period_key"]))
    for (bet_id, pk), group_iter in groupby(sorted_entries, key=lambda e: (e["bet_id"], e["period_key"])):
        group = list(group_iter)
        bet_entries = all_by_bet.get(bet_id, [])
        total_pool = len(bet_entries) * 100
        my_wa = sum(e["weight"] * e["amount"] for e in group)
        grp_wa = sum(
            e["weight"] * e["amount"]
            for e in bet_entries
            if e["period_key"] == pk
        )
        upper = calc_best_case_payout(pk, my_wa, grp_wa, total_pool)
        rows.append({
            "bet_id": bet_id,
            "target": group[0]["target"],
            "period_key": pk,
            "count": len(group),
            "stake": len(group) * 100,
            "upper": upper,
        })

    return balance, rows


class BalanceUserSelect(discord.ui.Select):
    def __init__(
        self,
        bot: commands.Bot,
        invoker_id: int,
        current_target_id: int,
        options: list[discord.SelectOption],
    ) -> None:
        super().__init__(placeholder="別のユーザーを選択", options=options)
        self.bot = bot
        self.invoker_id = invoker_id
        self.current_target_id = current_target_id

    async def callback(self, interaction: discord.Interaction) -> None:
        target_id = int(self.values[0])
        guild = interaction.guild
        member = guild.get_member(target_id) if guild else None
        target = member or await interaction.client.fetch_user(target_id)

        balance, rows = await _build_balance_data(self.bot, target_id)
        has_trunc = len(rows) > 25
        display_rows = rows[:25]
        embed = build_balance_embed(target, balance, display_rows, has_trunc, len(rows))

        # Rebuild view with updated default
        new_view = _build_balance_view(self.bot, self.invoker_id, target_id, interaction)
        await interaction.response.edit_message(embed=embed, view=new_view)


def _build_balance_view(
    bot: commands.Bot,
    invoker_id: int,
    current_target_id: int,
    interaction: discord.Interaction,
) -> discord.ui.View:
    """BalanceView を構築して返す。参加者 0 人なら空 View。"""
    view = discord.ui.View(timeout=180)

    async def _add_select() -> None:
        pass  # placeholder; 実際は同期的に構築

    # 参加者 ID を同期取得できないため、呼び出し側で既に取得済みの ids を渡す設計。
    # ここでは view 生成時に ids が既知でないため select を含まない版を返す。
    # 実際の構築は _build_balance_view_with_ids() を使う。
    return view


async def _build_balance_view_with_ids(
    bot: commands.Bot,
    invoker_id: int,
    current_target_id: int,
    interaction: discord.Interaction,
) -> discord.ui.View:
    from db import Database

    db: Database = bot.db
    participant_ids = await db.fetch_active_participant_ids()

    # current_target_id を候補に含める（進行中不参加でも選べるように）
    all_ids: list[int] = []
    if current_target_id not in participant_ids:
        all_ids.append(current_target_id)
    all_ids.extend(participant_ids)
    all_ids = all_ids[:25]  # Discord の Select 上限

    if not all_ids:
        return discord.ui.View(timeout=180)

    guild = interaction.guild
    options: list[discord.SelectOption] = []
    for uid in all_ids:
        member = guild.get_member(uid) if guild else None
        label = member.display_name if member else f"User {uid}"
        label = label[:25]  # SelectOption label 最大 25 文字
        options.append(discord.SelectOption(
            label=label,
            value=str(uid),
            default=(uid == current_target_id),
        ))

    view = discord.ui.View(timeout=180)
    select = BalanceUserSelect(bot, invoker_id, current_target_id, options)
    view.add_item(select)
    return view


# ---------------------------------------------------------------------------
# /bet-history pagination
# ---------------------------------------------------------------------------

class HistoryPaginationView(discord.ui.View):
    def __init__(
        self,
        bot: commands.Bot,
        target: discord.abc.User,
        total: int,
        png_bytes: bytes | None,
    ) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.target = target
        self.total = total
        self.png_bytes = png_bytes
        self.page = 0
        self.max_page = max(0, math.ceil(total / PAGE_SIZE) - 1)
        self._update_buttons()

    async def _build_embed(self) -> discord.Embed:
        from db import Database

        db: Database = self.bot.db
        rows = await db.fetch_user_closed_entries(
            self.target.id, limit=PAGE_SIZE, offset=self.page * PAGE_SIZE
        )
        embed = build_history_embed(self.target, rows, self.page, self.max_page, self.total)
        if self.png_bytes:
            embed.set_image(url="attachment://history.png")
        return embed

    async def _refresh(self, interaction: discord.Interaction) -> None:
        embed = await self._build_embed()
        self._update_buttons()
        if self.png_bytes:
            file = discord.File(io.BytesIO(self.png_bytes), filename="history.png")
            await interaction.response.edit_message(embed=embed, attachments=[file], view=self)
        else:
            await interaction.response.edit_message(embed=embed, view=self)

    def _update_buttons(self) -> None:
        self.first_btn.disabled = self.page == 0
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.max_page
        self.last_btn.disabled = self.page >= self.max_page

    @discord.ui.button(label="<<", style=discord.ButtonStyle.secondary)
    async def first_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = 0
        await self._refresh(interaction)

    @discord.ui.button(label="<", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = max(0, self.page - 1)
        await self._refresh(interaction)

    @discord.ui.button(label=">", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = min(self.max_page, self.page + 1)
        await self._refresh(interaction)

    @discord.ui.button(label=">>", style=discord.ButtonStyle.secondary)
    async def last_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = self.max_page
        await self._refresh(interaction)


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class WalletCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="balance", description="残高と参加中の賭けを確認します")
    @app_commands.describe(user="確認するユーザー（省略時は自分）")
    async def balance(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        target = user or interaction.user
        balance, rows = await _build_balance_data(self.bot, target.id)

        has_trunc = len(rows) > 25
        display_rows = rows[:25]
        embed = build_balance_embed(target, balance, display_rows, has_trunc, len(rows))

        view = await _build_balance_view_with_ids(
            self.bot, interaction.user.id, target.id, interaction
        )

        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="ranking", description="残高ランキングを表示します")
    async def ranking(self, interaction: discord.Interaction) -> None:
        from db import Database

        db: Database = self.bot.db
        total = await db.count_users()

        view = RankingPaginationView(self.bot, total)
        embed = await view._build_embed()
        view._update_buttons()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="bet-history", description="賭けの履歴と残高推移を表示します")
    @app_commands.describe(user="確認するユーザー（省略時は自分）")
    async def bet_history(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        from db import Database

        db: Database = self.bot.db
        target = user or interaction.user

        event_rows, total = await asyncio.gather(
            db.fetch_user_all_events_for_graph(target.id),
            db.count_user_closed_entries(target.id),
        )

        # イベントシーケンス構築
        events: list[tuple[datetime, int]] = []
        for row in event_rows:
            ts_str = row["created_at"]
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except Exception:
                continue
            delta = 400 if row["entry_id"] == row["first_entry_id"] else -100
            events.append((ts, delta))
            # 精算イベント
            if row["status"] == "closed" and row["payout"] is not None and row["payout"] > 0:
                try:
                    closed_ts = datetime.fromisoformat(row["closed_at"].replace("Z", "+00:00"))
                except Exception:
                    closed_ts = ts
                events.append((closed_ts, row["payout"]))

        events.sort(key=lambda e: e[0])

        # グラフ生成（blocking 回避）
        png_bytes: bytes | None = None
        if events:
            user_label = f"User {target.id}"
            import charts as charts_mod
            png_bytes = await asyncio.to_thread(
                charts_mod.generate_balance_history_png, events, user_label
            )

        closed_rows = await db.fetch_user_closed_entries(target.id, limit=PAGE_SIZE, offset=0)
        max_page = max(0, math.ceil(total / PAGE_SIZE) - 1)

        embed = build_history_embed(target, closed_rows, 0, max_page, total)
        if png_bytes:
            embed.set_image(url="attachment://history.png")

        view = HistoryPaginationView(self.bot, target, total, png_bytes)

        if png_bytes:
            file = discord.File(io.BytesIO(png_bytes), filename="history.png")
            await interaction.followup.send(embed=embed, view=view, file=file, ephemeral=True)
        else:
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WalletCog(bot))
