from __future__ import annotations

from typing import Any

import discord

from odds import PERIOD_KEYS, PERIOD_LABELS, PERIOD_MULT, PERIOD_SECONDS, live_periods


def _elapsed_str(elapsed_sec: float) -> str:
    days = int(elapsed_sec // 86400)
    hours = int((elapsed_sec % 86400) // 3600)
    minutes = int((elapsed_sec % 3600) // 60)
    if days > 0:
        return f"{days}日{hours}時間{minutes}分"
    if hours > 0:
        return f"{hours}時間{minutes}分"
    return f"{minutes}分"


def build_bet_embed(
    bet: Any,  # aiosqlite.Row
    entries: list[Any],  # aiosqlite.Row list
    live_period_keys: list[str],
) -> discord.Embed:
    dead_set = set(PERIOD_KEYS) - set(live_period_keys)

    embed = discord.Embed(
        title=f"賭け #{bet['bet_id']}: {bet['target']}",
        color=discord.Color.blue(),
    )
    embed.add_field(
        name="作成者",
        value=f"<@{bet['creator_id']}>",
        inline=True,
    )
    embed.add_field(
        name="作成日時",
        value=f"<t:{_iso_to_unix(bet['created_at'])}:R>",
        inline=True,
    )

    # Period table
    lines = []
    for pk in PERIOD_KEYS:
        period_entries = [e for e in entries if e["period_key"] == pk]
        count = len(period_entries)
        total = count * 100
        label = PERIOD_LABELS[pk]
        mult = PERIOD_MULT[pk]
        if pk in dead_set:
            status = "❌負け確定"
        else:
            status = "✅"
        lines.append(f"{status} **{label}** ({mult}x)  |  {count}口 / {total}P")

    embed.add_field(
        name="期間別状況",
        value="\n".join(lines) if lines else "—",
        inline=False,
    )

    # Participants (up to 10, then "他 N 名")
    unique_users = list(dict.fromkeys(e["user_id"] for e in entries))
    if unique_users:
        display = [f"<@{uid}>" for uid in unique_users[:10]]
        remainder = len(unique_users) - 10
        if remainder > 0:
            display.append(f"他 {remainder} 名")
        embed.add_field(
            name=f"参加者 ({len(unique_users)}名)",
            value=" ".join(display),
            inline=False,
        )

    total_pool = len(entries) * 100
    embed.set_footer(text=f"プール総額: {total_pool}P  |  参加: 100P → +500P ボーナス")
    return embed


def build_result_embed(
    bet: Any,
    entries: list[Any],
    winners: list[str],
    elapsed_sec: float,
    k: float,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"賭け #{bet['bet_id']} 結果: {bet['target']}",
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="経過時間",
        value=_elapsed_str(elapsed_sec),
        inline=True,
    )
    winner_labels = "・".join(PERIOD_LABELS[w] for w in winners)
    embed.add_field(name="勝ち期間", value=winner_labels, inline=True)
    embed.add_field(name="精度係数 k", value=f"{k:.3f}", inline=True)

    # Winners
    winner_set = set(winners)
    won = [e for e in entries if e["period_key"] in winner_set and (e["payout"] or 0) > 0]
    lost = [e for e in entries if e["period_key"] not in winner_set]

    if won:
        winner_lines = [
            f"<@{e['user_id']}> ({PERIOD_LABELS[e['period_key']]}) → **{e['payout']}P**"
            for e in won
        ]
        # Truncate for embed field limit
        text = "\n".join(winner_lines[:15])
        if len(won) > 15:
            text += f"\n他 {len(won)-15} 名"
        embed.add_field(name="勝者", value=text, inline=False)
    else:
        embed.add_field(name="勝者", value="なし（賭け金返金）", inline=False)

    if lost:
        loser_lines = [
            f"<@{e['user_id']}> ({PERIOD_LABELS[e['period_key']]})"
            for e in lost[:10]
        ]
        text = " ".join(loser_lines)
        if len(lost) > 10:
            text += f" 他 {len(lost)-10} 名"
        embed.add_field(name="敗者", value=text, inline=False)

    total_pool = len(entries) * 100
    embed.set_footer(text=f"プール総額: {total_pool}P")
    return embed


def build_participation_embed(
    bet_id: int,
    period_key: str,
    new_balance: int,
    first_time: bool = True,
) -> discord.Embed:
    label = PERIOD_LABELS[period_key]
    if first_time:
        desc = (
            f"賭け **#{bet_id}** の **{label}** に参加しました。\n"
            f"−100P（賭け金）＋500P（初回ボーナス）= **+400P 純増**\n"
            f"現在残高: **{new_balance}P**"
        )
    else:
        desc = (
            f"賭け **#{bet_id}** の **{label}** に **追加参加** しました。\n"
            f"−100P（賭け金のみ、ボーナスは初回のみ）\n"
            f"現在残高: **{new_balance}P**"
        )
    return discord.Embed(title="参加完了", description=desc, color=discord.Color.green())


def _iso_to_unix(iso: str) -> int:
    """Convert ISO8601 UTC string to Unix timestamp."""
    from datetime import datetime, timezone
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())
