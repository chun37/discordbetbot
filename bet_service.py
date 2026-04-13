"""
Neutral service layer — imported by both cogs/ and views/.
Must NOT import from cogs/ or views/ to avoid circular imports.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import discord

import odds as odds_module
from embeds import build_bet_embed, build_participation_embed, build_result_embed

if TYPE_CHECKING:
    from embed_refresher import EmbedRefresher
    from scheduler import Scheduler

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class BetError(Exception):
    """Base class for bet service errors."""


class BetNotFound(BetError):
    pass


class BetAlreadyClosed(BetError):
    pass


class NotAllowed(BetError):
    pass


class PeriodEliminated(BetError):
    pass


@dataclass
class ParticipationResult:
    entry_id: int
    period_key: str
    new_balance: int
    first_time: bool


@dataclass
class SettleResult:
    bet_id: int
    winners: list[str]
    elapsed_sec: float
    k: float


async def create_bet(
    bot: Any,
    creator_id: int,
    target: str,
    channel: discord.TextChannel,
) -> int:
    from db import Database
    from scheduler import Scheduler
    from embed_refresher import EmbedRefresher

    db: Database = bot.db
    scheduler: Scheduler = bot.scheduler

    created_at = _utcnow().isoformat()
    bet_id = await db.create_bet(creator_id, target, channel.id, created_at)

    # Build initial embed (no entries, all periods live)
    all_periods = list(odds_module.PERIOD_KEYS)
    fake_bet = {
        "bet_id": bet_id,
        "creator_id": creator_id,
        "target": target,
        "created_at": created_at,
    }
    embed = build_bet_embed(fake_bet, [], all_periods)

    # Import view lazily to avoid circular import
    from views.bet_main import build_bet_view

    view = build_bet_view(bet_id)
    message = await channel.send(embed=embed, view=view)
    await db.update_bet_message_id(bet_id, message.id)
    await scheduler.schedule_for_new_bet(bet_id, created_at)

    logger.info("Bet #%d created by user %d: %s", bet_id, creator_id, target)
    return bet_id


async def join_bet(
    bot: Any,
    bet_id: int,
    user_id: int,
    period_key: str,
) -> ParticipationResult:
    from db import Database
    from embed_refresher import EmbedRefresher

    db: Database = bot.db
    refresher: EmbedRefresher = bot.refresher

    now = _utcnow().isoformat()

    async def _tx(conn):
        # Fetch bet inside transaction
        async with conn.execute(
            "SELECT * FROM bets WHERE bet_id=?", (bet_id,)
        ) as cur:
            bet = await cur.fetchone()

        if bet is None:
            raise BetNotFound(f"Bet #{bet_id} not found")
        if bet["status"] != "open":
            raise BetAlreadyClosed(f"Bet #{bet_id} is already closed")

        # First-time-per-bet detection (inside tx for consistency)
        async with conn.execute(
            "SELECT 1 FROM entries WHERE bet_id=? AND user_id=? LIMIT 1",
            (bet_id, user_id),
        ) as cur:
            first_time = (await cur.fetchone()) is None

        live = await db.fetch_live_periods_tx(conn, bet_id)
        if period_key not in live:
            raise PeriodEliminated(f"Period {period_key} has already been eliminated")

        weight = odds_module.calc_weight(len(live))

        entry_id = await db.insert_entry_tx(
            conn, bet_id, user_id, period_key, 100, weight, now
        )
        # First join: +500 bonus - 100 stake = +400 net; subsequent: -100 only
        delta = 400 if first_time else -100
        await db.upsert_balance_tx(conn, user_id, delta)

        return entry_id, bet["channel_id"], first_time

    entry_id, channel_id, first_time = await db.execute_write(_tx)

    # Read new balance outside tx
    new_balance = await db.fetch_balance(user_id)
    refresher.schedule(channel_id, bet_id)

    logger.info(
        "User %d joined bet #%d period=%s entry_id=%d first_time=%s",
        user_id, bet_id, period_key, entry_id, first_time,
    )
    return ParticipationResult(
        entry_id=entry_id,
        period_key=period_key,
        new_balance=new_balance,
        first_time=first_time,
    )


async def close_bet(
    bot: Any,
    bet_id: int,
    actor_user_id: int,
) -> SettleResult:
    from db import Database
    from scheduler import Scheduler

    db: Database = bot.db
    scheduler: Scheduler = bot.scheduler

    bet = await db.fetch_bet(bet_id)
    if bet is None:
        raise BetNotFound(f"Bet #{bet_id} not found")
    if bet["status"] != "open":
        raise BetAlreadyClosed(f"Bet #{bet_id} is already closed")
    if bet["creator_id"] != actor_user_id:
        raise NotAllowed("Only the creator can close this bet")

    created_dt = datetime.fromisoformat(bet["created_at"].replace("Z", "+00:00"))
    if created_dt.tzinfo is None:
        from datetime import timezone as _tz
        created_dt = created_dt.replace(tzinfo=_tz.utc)

    now = _utcnow()
    elapsed_sec = max(0.0, (now - created_dt).total_seconds())
    closed_at = now.isoformat()

    entries_rows = await db.fetch_bet_entries(bet_id)
    live_keys = await db.fetch_live_periods_tx(db.conn, bet_id)

    winners = odds_module.find_winners(elapsed_sec, live_keys)
    total_pool = len(entries_rows) * 100

    entry_inputs = [
        odds_module.EntryInput(
            entry_id=e["entry_id"],
            period_key=e["period_key"],
            amount=e["amount"],
            weight=e["weight"],
        )
        for e in entries_rows
    ]

    payouts = odds_module.calc_payouts(entry_inputs, winners, elapsed_sec, total_pool)

    # Determine k for result display
    if winners:
        w_sec = odds_module.PERIOD_SECONDS[winners[0]]
        k = min(w_sec, elapsed_sec) / max(w_sec, elapsed_sec) if max(w_sec, elapsed_sec) > 0 else 0.0
    else:
        k = 0.0

    async def _settle_tx(conn):
        for e in entries_rows:
            payout = payouts.get(e["entry_id"], 0)
            await db.update_entry_payout_tx(conn, e["entry_id"], payout)
            if payout > 0:
                await db.upsert_balance_tx(conn, e["user_id"], payout)

        await db.close_bet_tx(conn, bet_id, closed_at, int(elapsed_sec), winners)
        await db.mark_schedules_fired_for_bet_tx(conn, bet_id)

    await db.execute_write(_settle_tx)

    # Cancel scheduler tasks
    scheduler.cancel_for_bet(bet_id)

    # Fetch updated entries for result embed
    settled_entries = await db.fetch_bet_entries(bet_id)

    # Build result embed and edit the original message
    result_embed = build_result_embed(bet, settled_entries, winners, elapsed_sec, k)

    channel = bot.get_channel(bet["channel_id"])
    if channel is None:
        try:
            channel = await bot.fetch_channel(bet["channel_id"])
        except Exception:
            logger.warning("Cannot find channel %s for bet #%d", bet["channel_id"], bet_id)
            channel = None

    if channel:
        try:
            message = await channel.fetch_message(bet["message_id"])
            await message.edit(embed=result_embed, view=None)
        except Exception:
            logger.exception("Failed to edit original message for bet #%d", bet_id)
        # Post a fresh public message so users get a new-message notification
        try:
            await channel.send(
                content=f"⏰ 賭け **#{bet_id}** が終了しました",
                embed=result_embed,
            )
        except Exception:
            logger.warning("Failed to send public result for bet #%d", bet_id)

    logger.info(
        "Bet #%d closed by user %d — elapsed=%.0fs winners=%s",
        bet_id, actor_user_id, elapsed_sec, winners,
    )
    return SettleResult(bet_id=bet_id, winners=winners, elapsed_sec=elapsed_sec, k=k)
