"""Pure domain logic — no I/O, no async, no DB, no Discord."""
from __future__ import annotations

from datetime import datetime

from domain.models import (
    Bet,
    BetAlreadyClosed,
    Entry,
    JoinDecision,
    NotAllowed,
    PeriodEliminated,
    SettleDecision,
)
from domain.odds import EntryInput, calc_payouts, calc_weight, find_winners, PERIOD_SECONDS


STAKE = 100
FIRST_TIME_BONUS = 500


def validate_join(
    bet: Bet,
    entries: list[Entry],
    live_periods: list[str],
    user_id: int,
    period_key: str,
) -> JoinDecision:
    """Validate a join request and return the decision (weight, delta, etc.).

    ``entries`` should be all existing entries for this bet (used to detect
    whether *user_id* is joining for the first time).
    """
    if bet.status != "open":
        raise BetAlreadyClosed(f"Bet #{bet.bet_id} is already closed")

    if period_key not in live_periods:
        raise PeriodEliminated(f"Period {period_key} has already been eliminated")

    first_time = all(e.user_id != user_id for e in entries)
    weight = calc_weight(len(live_periods))
    delta = (FIRST_TIME_BONUS - STAKE) if first_time else -STAKE

    return JoinDecision(
        weight=weight,
        amount=STAKE,
        balance_delta=delta,
        first_time=first_time,
    )


def settle(
    bet: Bet,
    entries: list[Entry],
    live_periods: list[str],
    actor_user_id: int,
    now: datetime,
) -> SettleDecision:
    """Compute settlement: winners, payouts, k-factor.

    Pure calculation — does NOT mutate any entry or persist anything.
    """
    if bet.status != "open":
        raise BetAlreadyClosed(f"Bet #{bet.bet_id} is already closed")
    if bet.creator_id != actor_user_id:
        raise NotAllowed("Only the creator can close this bet")

    elapsed_sec = max(0.0, (now - bet.created_at).total_seconds())

    winners = find_winners(elapsed_sec, live_periods)
    total_pool = len(entries) * STAKE

    entry_inputs = [
        EntryInput(
            entry_id=e.entry_id,
            period_key=e.period_key,
            amount=e.amount,
            weight=e.weight,
        )
        for e in entries
    ]

    payouts = calc_payouts(entry_inputs, winners, elapsed_sec, total_pool)

    if winners:
        w_sec = PERIOD_SECONDS[winners[0]]
        denom = max(w_sec, elapsed_sec)
        k = min(w_sec, elapsed_sec) / denom if denom > 0 else 0.0
    else:
        k = 0.0

    return SettleDecision(
        winners=winners,
        payouts=payouts,
        elapsed_sec=elapsed_sec,
        k=k,
    )
