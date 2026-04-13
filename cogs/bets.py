from __future__ import annotations

import json
import logging

import discord
from discord import app_commands
from discord.ext import commands

import bet_service
from odds import PERIOD_SECONDS

logger = logging.getLogger(__name__)


class BetListView(discord.ui.View):
    def __init__(self, bot: commands.Bot, bets: list) -> None:
        super().__init__(timeout=120)
        self.bot = bot
        options = [
            discord.SelectOption(
                label=f"#{b['bet_id']} {b['target'][:80] or '(無題)'}",
                value=str(b["bet_id"]),
            )
            for b in bets[:25]
        ]
        select = discord.ui.Select(
            placeholder="再投稿する賭けを選択",
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self._on_select
        self.add_item(select)
        self._select = select

    async def _on_select(self, interaction: discord.Interaction) -> None:
        from db import Database
        from embeds import build_bet_embed, build_result_embed
        from views.bet_main import build_bet_view

        await interaction.response.defer()

        bet_id = int(interaction.data["values"][0])
        db: Database = self.bot.db

        bet = await db.fetch_bet(bet_id)
        if bet is None or bet["status"] != "open":
            await interaction.edit_original_response(
                content="この賭けはすでに進行中ではありません。", view=None,
            )
            return

        # Save old location before repost
        old_channel_id = bet["channel_id"]
        old_message_id = bet["message_id"]

        entries = await db.fetch_bet_entries(bet_id)
        live = await db.fetch_live_periods_tx(db.conn, bet_id)
        embed = build_bet_embed(bet, entries, live)
        view = build_bet_view(bet_id)

        # Post new message and update canonical message_id
        new_msg = await interaction.channel.send(embed=embed, view=view)
        await db.update_bet_message_id(bet_id, new_msg.id)

        # TOCTOU: if close_bet ran during the repost, sync the new message to result
        bet_after = await db.fetch_bet(bet_id)
        if bet_after and bet_after["status"] == "closed":
            try:
                settled_entries = await db.fetch_bet_entries(bet_id)
                winners = json.loads(bet_after["winning_periods"] or "[]")
                elapsed = bet_after["elapsed_seconds"] or 0
                k = (
                    min(PERIOD_SECONDS[winners[0]], elapsed)
                    / max(PERIOD_SECONDS[winners[0]], elapsed, 1)
                    if winners else 0.0
                )
                result_embed = build_result_embed(
                    bet_after, settled_entries, winners, float(elapsed), k,
                )
                await new_msg.edit(embed=result_embed, view=None)
            except Exception:
                logger.exception("TOCTOU resync failed for bet #%d", bet_id)

        # Neutralize the old message so its buttons don't become zombies
        try:
            old_ch = (
                self.bot.get_channel(old_channel_id)
                or await self.bot.fetch_channel(old_channel_id)
            )
            old_msg = await old_ch.fetch_message(old_message_id)
            await old_msg.edit(
                content=f"→ この賭けは下に再投稿されました (bet #{bet_id})",
                view=None,
            )
        except Exception:
            logger.warning(
                "Could not neutralize old message %s for bet #%d", old_message_id, bet_id
            )

        await interaction.edit_original_response(
            content=f"賭け **#{bet_id}** を再投稿しました（以後の更新は新メッセージに反映されます）。",
            view=None,
        )


class BetsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="bet-create", description="新しい賭けを作成します")
    @app_commands.describe(target="賭けの対象（自由記述）")
    async def bet_create(
        self,
        interaction: discord.Interaction,
        target: str,
    ) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            bet_id = await bet_service.create_bet(
                self.bot,
                interaction.user.id,
                target,
                interaction.channel,
            )
        except Exception:
            logger.exception("bet_create failed for user %d", interaction.user.id)
            await interaction.followup.send("賭けの作成に失敗しました。", ephemeral=True)
            return

        await interaction.followup.send(
            f"賭け **#{bet_id}** を作成しました！",
            ephemeral=True,
        )

    @app_commands.command(name="bet-list", description="進行中の賭け一覧を表示します")
    async def bet_list(self, interaction: discord.Interaction) -> None:
        from db import Database

        db: Database = self.bot.db
        bets = await db.fetch_open_bets()

        if not bets:
            await interaction.response.send_message("進行中の賭けはありません。", ephemeral=True)
            return

        embed = discord.Embed(title="進行中の賭け一覧", color=discord.Color.blue())
        lines = []
        for b in bets[:25]:
            guild_id = interaction.guild_id or 0
            jump = f"https://discord.com/channels/{guild_id}/{b['channel_id']}/{b['message_id']}"
            lines.append(f"**#{b['bet_id']}** [{b['target']}]({jump})")
        embed.description = "\n".join(lines)
        if len(bets) > 25:
            embed.set_footer(text=f"表示は先頭 25 件まで（全 {len(bets)} 件）")

        view = BetListView(self.bot, bets)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BetsCog(bot))
