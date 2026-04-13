from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands


class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="help", description="BOTの使い方とコマンド一覧を表示します")
    async def help_cmd(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="BetBot ヘルプ",
            color=discord.Color.blue(),
        )
        embed.add_field(
            name="概要",
            value=(
                "予測対象イベント（例: 「雨が降る」）が**いつ起きるか**を、\n"
                "1日 / 3日 / 1週間 / 2週間 / 1か月 / 3か月 / 半年 / 1年 の 8 期間から選んで賭けます。\n"
                "目標イベントが実際に起きた時点で、その経過時間に**最も近い期間が勝ち**です。"
            ),
            inline=False,
        )
        embed.add_field(
            name="賭け方・報酬",
            value=(
                "・1 口 **100P**（各 bet への**初回参加は +500P ボーナス**）\n"
                "・period の milestone（1日=86400秒 等）を過ぎると ❌ 負け確定\n"
                "・配当 = `k × mult × pool × 自分の重み率`\n"
                "　`k` = milestone までの近さ（0〜1）, `mult` = 期間倍率（1日×1.0〜1年×15.0）\n"
                "・早い段階で参加するほど weight が高く有利"
            ),
            inline=False,
        )
        embed.add_field(
            name="期間倍率",
            value=(
                "`1日` ×1.0　`3日` ×1.3　`1週間` ×1.7　`2週間` ×2.2\n"
                "`1か月` ×3.0　`3か月` ×5.0　`半年` ×8.0　`1年` ×15.0"
            ),
            inline=False,
        )
        embed.add_field(
            name="コマンド一覧",
            value=(
                "`/bet-create` — 新しい賭けを作成\n"
                "`/bet-list` — 進行中の賭け一覧\n"
                "`/balance` — 残高・参加中の賭けを確認（他ユーザー指定可、プルダウンで切替可）\n"
                "`/bet-history` — 賭け履歴と残高推移グラフ（他ユーザー指定可）\n"
                "`/ranking` — 残高ランキング\n"
                "`/help` — このヘルプ"
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HelpCog(bot))
