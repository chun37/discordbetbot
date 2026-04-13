from __future__ import annotations

import unicodedata
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


# ---------------------------------------------------------------------------
# Unicode-aware column formatting helpers
# ---------------------------------------------------------------------------

def _visual_width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in ("F", "W", "A") else 1 for c in s)


def _pad(s: str, width: int) -> str:
    return s + " " * max(0, width - _visual_width(s))


def _truncate(s: str, width: int) -> str:
    if _visual_width(s) <= width:
        return s
    out, w = "", 0
    for c in s:
        cw = 2 if unicodedata.east_asian_width(c) in ("F", "W", "A") else 1
        if w + cw > width - 1:
            return out + "…"
        out += c
        w += cw
    return out


# ---------------------------------------------------------------------------
# Balance & history embeds
# ---------------------------------------------------------------------------

def build_balance_embed(
    target: discord.abc.User,
    balance: int,
    rows: list[dict],
    has_truncation: bool = False,
    total_rows: int = 0,
) -> discord.Embed:
    """残高 + 参加中賭け一覧 embed。rows は (bet_id, period_key, count, stake, upper) の dict リスト。"""
    embed = discord.Embed(
        title=f"残高・参加中の賭け — {target.display_name}",
        color=discord.Color.green(),
    )

    bal_line = f"{target.mention} の残高: **{balance}P**"

    if not rows:
        embed.description = bal_line + "\n\n参加中の賭けはありません。"
    else:
        # Header
        header = (
            _pad("#ID", 5)
            + _pad("対象", 15)
            + _pad("期間", 6)
            + _pad("口数", 5)
            + _pad("賭金", 6)
            + "上限"
        )
        sep = "─" * _visual_width(header)
        lines = [header, sep]

        for r in rows:
            target_str = _truncate(r["target"], 14)
            period_label = PERIOD_LABELS.get(r["period_key"], r["period_key"])
            line = (
                _pad(f"#{r['bet_id']}", 5)
                + _pad(target_str, 15)
                + _pad(period_label, 6)
                + _pad(str(r["count"]) + "口", 5)
                + _pad(str(r["stake"]) + "P", 6)
                + f"+{r['upper']}P"
            )
            lines.append(line)

        # Totals
        total_stake = sum(r["stake"] for r in rows)
        total_upper = sum(r["upper"] for r in rows)
        lines.append(sep)
        lines.append(
            _pad("", 5) + _pad("", 15) + _pad("", 6)
            + _pad("計", 5)
            + _pad(str(total_stake) + "P", 6)
            + f"+{total_upper}P"
        )

        table = "```\n" + "\n".join(lines) + "\n```"
        embed.description = bal_line + "\n" + table

    if has_truncation:
        footer = f"表示は先頭 25 行まで（全 {total_rows} 行）  ※ 上限は k=1 の最大値"
    else:
        footer = "※ 上限は k=1・現状参加状況での最大値。実際は常にこれ以下"
    embed.set_footer(text=footer)
    return embed


def build_history_embed(
    target: discord.abc.User,
    rows: list[Any],
    page: int,
    max_page: int,
    total: int,
) -> discord.Embed:
    """賭け履歴テーブル embed。rows は fetch_user_closed_entries の Row リスト（1 ページ分）。"""
    from datetime import datetime, timezone

    embed = discord.Embed(
        title=f"賭け履歴 — {target.display_name}",
        color=discord.Color.blue(),
    )

    if not rows:
        embed.description = "履歴がありません。"
    else:
        header = (
            _pad("#ID", 5)
            + _pad("対象", 15)
            + _pad("期間", 6)
            + _pad("賭金", 6)
            + _pad("払戻", 6)
            + _pad("損益", 7)
            + "時刻"
        )
        sep = "─" * _visual_width(header)
        lines = [header, sep]

        for r in rows:
            payout = r["payout"] or 0
            pl = payout - r["amount"]
            pl_str = f"+{pl}P" if pl >= 0 else f"{pl}P"
            # Parse closed_at
            closed_str = r["closed_at"] or ""
            try:
                dt = datetime.fromisoformat(closed_str.replace("Z", "+00:00"))
                date_label = dt.strftime("%m/%d")
            except Exception:
                date_label = "?"
            target_str = _truncate(r["target"], 14)
            period_label = PERIOD_LABELS.get(r["period_key"], r["period_key"])
            line = (
                _pad(f"#{r['bet_id']}", 5)
                + _pad(target_str, 15)
                + _pad(period_label, 6)
                + _pad(str(r["amount"]) + "P", 6)
                + _pad(str(payout) + "P", 6)
                + _pad(pl_str, 7)
                + date_label
            )
            lines.append(line)

        embed.description = "```\n" + "\n".join(lines) + "\n```"

    footer = f"ページ {page + 1}/{max_page + 1}  (全 {total} 件)  ※ 損益 = 払戻 − 賭金"
    embed.set_footer(text=footer)
    return embed


def _iso_to_unix(iso: str) -> int:
    """Convert ISO8601 UTC string to Unix timestamp."""
    from datetime import datetime, timezone
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())
