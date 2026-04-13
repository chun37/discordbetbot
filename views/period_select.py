from __future__ import annotations

import logging
from typing import Any

import discord
from discord import ui

import bet_service
from odds import PERIOD_KEYS, PERIOD_LABELS, PERIOD_MULT

logger = logging.getLogger(__name__)


class PeriodSelectView(ui.View):
    def __init__(self, bot: Any, bet_id: int, live_periods: list[str]) -> None:
        super().__init__(timeout=120)
        self.bet_id = bet_id
        self._history: list[str] = []
        options = [
            discord.SelectOption(
                label=f"{PERIOD_LABELS[pk]}  ({PERIOD_MULT[pk]}x)",
                value=pk,
            )
            for pk in PERIOD_KEYS
            if pk in live_periods
        ]
        select = ui.Select(
            placeholder="期間を選択",
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self._select_callback
        self.add_item(select)
        self._select = select

    @classmethod
    async def create(cls, bot: Any, bet_id: int) -> "PeriodSelectView | None":
        """
        Factory: fetch live periods from DB and build the view.
        Returns None if the bet is closed or has no live periods.
        """
        from db import Database

        db: Database = bot.db
        bet = await db.fetch_bet(bet_id)
        if bet is None or bet["status"] != "open":
            return None

        live = await db.fetch_live_periods_tx(db.conn, bet_id)
        if not live:
            return None

        return cls(bot, bet_id, live)

    async def _select_callback(self, interaction: discord.Interaction) -> None:
        # ① Defer first to secure the 3-second ACK deadline.
        # On a component interaction, bare defer() = DEFERRED_UPDATE_MESSAGE:
        # the message stays as-is; subsequent edits use edit_original_response().
        await interaction.response.defer()

        period_key = interaction.data["values"][0]

        # ② Temporarily disable the select to prevent double-clicks
        self._select.disabled = True
        try:
            await interaction.edit_original_response(view=self)
        except Exception:
            pass  # best-effort; processing continues regardless

        try:
            result = await bet_service.join_bet(
                interaction.client,
                self.bet_id,
                interaction.user.id,
                period_key,
            )
        except bet_service.PeriodEliminated:
            # Period may have just been eliminated — rebuild select with fresh live list
            from db import Database
            db: Database = interaction.client.db
            live = await db.fetch_live_periods_tx(db.conn, self.bet_id)
            if live:
                new_view = PeriodSelectView(interaction.client, self.bet_id, live)
                new_view._history = list(self._history)
                self.stop()
                await interaction.edit_original_response(
                    content=f"⚠️ {PERIOD_LABELS[period_key]} はすでに負け確定です。別の期間を選んでください。",
                    embed=None,
                    view=new_view,
                )
            else:
                self.stop()
                await interaction.edit_original_response(
                    content="参加可能な期間がありません。",
                    embed=None,
                    view=None,
                )
            return
        except bet_service.BetAlreadyClosed:
            self.stop()
            await interaction.edit_original_response(
                content="この賭けはすでに終了しています。",
                embed=None,
                view=None,
            )
            return
        except Exception:
            logger.exception("join_bet failed bet=%d user=%d", self.bet_id, interaction.user.id)
            self._select.disabled = False
            await interaction.edit_original_response(
                content="エラーが発生しました。",
                view=self,
            )
            return

        # ③ Success — append to history, re-enable select, update ephemeral in-place
        self._history.append(period_key)
        self._select.disabled = False

        from embeds import build_participation_embed
        embed = build_participation_embed(
            self.bet_id, result.period_key, result.new_balance, result.first_time,
        )
        hist_line = " / ".join(PERIOD_LABELS[k] for k in self._history)
        embed.set_footer(text=f"今回セッションの追加: {hist_line}")

        await interaction.edit_original_response(
            content="続けて下のメニューから追加できます（2 分で自動終了）",
            embed=embed,
            view=self,
        )
