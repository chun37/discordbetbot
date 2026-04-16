"""
Application service layer — orchestrates domain logic, DB, and Discord.

Must NOT import from cogs/ or views/ to avoid circular imports.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import discord

import odds as odds_module
from domain.models import (
    Bet,
    BetAlreadyClosed,
    BetError,
    BetNotFound,
    Entry,
    NotAllowed,
    PeriodEliminated,
)
from domain.services import settle as domain_settle
from domain.services import validate_join as domain_validate_join
from embeds import build_bet_embed, build_result_embed

if TYPE_CHECKING:
    from embed_refresher import EmbedRefresher
    from scheduler import Scheduler

logger = logging.getLogger(__name__)

# Re-export domain exceptions so existing callers (views, cogs) keep working.
__all__ = [
    "BetError",
    "BetNotFound",
    "BetAlreadyClosed",
    "NotAllowed",
    "PeriodEliminated",
    "ParticipationResult",
    "SettleResult",
    "create_bet",
    "join_bet",
    "close_bet",
]


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_datetime(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _row_to_bet(row: Any) -> Bet:
    return Bet(
        bet_id=row["bet_id"],
        creator_id=row["creator_id"],
        target=row["target"],
        created_at=_parse_datetime(row["created_at"]),
        status=row["status"],
    )


def _rows_to_entries(rows: list[Any]) -> list[Entry]:
    return [
        Entry(
            entry_id=r["entry_id"],
            bet_id=r["bet_id"],
            user_id=r["user_id"],
            period_key=r["period_key"],
            amount=r["amount"],
            weight=r["weight"],
            payout=r["payout"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Application service functions
# ---------------------------------------------------------------------------

async def create_bet(
    bot: Any,
    creator_id: int,
    target: str,
    channel: discord.TextChannel,
) -> int:
    from db import Database
    from scheduler import Scheduler

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
            bet_row = await cur.fetchone()

        if bet_row is None:
            raise BetNotFound(f"Bet #{bet_id} not found")

        bet = _row_to_bet(bet_row)

        # Fetch existing entries for first-time detection
        async with conn.execute(
            "SELECT * FROM entries WHERE bet_id=?", (bet_id,)
        ) as cur:
            entry_rows = await cur.fetchall()

        entries = _rows_to_entries(entry_rows)
        live = await db.fetch_live_periods_tx(conn, bet_id)

        # --- Domain logic (pure) ---
        decision = domain_validate_join(bet, entries, live, user_id, period_key)

        # --- Persist ---
        entry_id = await db.insert_entry_tx(
            conn, bet_id, user_id, period_key, decision.amount, decision.weight, now
        )
        await db.upsert_balance_tx(conn, user_id, decision.balance_delta)

        return entry_id, bet_row["channel_id"], decision.first_time

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

    bet_row = await db.fetch_bet(bet_id)
    if bet_row is None:
        raise BetNotFound(f"Bet #{bet_id} not found")

    bet = _row_to_bet(bet_row)
    now = _utcnow()

    entries_rows = await db.fetch_bet_entries(bet_id)
    entries = _rows_to_entries(entries_rows)
    live_keys = await db.fetch_live_periods_tx(db.conn, bet_id)

    # --- Domain logic (pure) ---
    decision = domain_settle(bet, entries, live_keys, actor_user_id, now)

    # --- Persist ---
    closed_at = now.isoformat()

    async def _settle_tx(conn):
        for e in entries_rows:
            payout = decision.payouts.get(e["entry_id"], 0)
            await db.update_entry_payout_tx(conn, e["entry_id"], payout)
            if payout > 0:
                await db.upsert_balance_tx(conn, e["user_id"], payout)

        await db.close_bet_tx(
            conn, bet_id, closed_at, int(decision.elapsed_sec), decision.winners
        )
        await db.mark_schedules_fired_for_bet_tx(conn, bet_id)

    await db.execute_write(_settle_tx)

    # Cancel scheduler tasks
    scheduler.cancel_for_bet(bet_id)

    # Fetch updated entries for result embed
    settled_entries = await db.fetch_bet_entries(bet_id)

    # Build result embed and edit the original message
    result_embed = build_result_embed(
        bet_row, settled_entries, decision.winners, decision.elapsed_sec, decision.k
    )

    channel = bot.get_channel(bet_row["channel_id"])
    if channel is None:
        try:
            channel = await bot.fetch_channel(bet_row["channel_id"])
        except Exception:
            logger.warning(
                "Cannot find channel %s for bet #%d", bet_row["channel_id"], bet_id
            )
            channel = None

    if channel:
        try:
            message = await channel.fetch_message(bet_row["message_id"])
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
        bet_id, actor_user_id, decision.elapsed_sec, decision.winners,
    )
    return SettleResult(
        bet_id=bet_id,
        winners=decision.winners,
        elapsed_sec=decision.elapsed_sec,
        k=decision.k,
    )
