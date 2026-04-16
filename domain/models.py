from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class BetError(Exception):
    """Base class for bet domain errors."""


class BetNotFound(BetError):
    pass


class BetAlreadyClosed(BetError):
    pass


class NotAllowed(BetError):
    pass


class PeriodEliminated(BetError):
    pass


# ---------------------------------------------------------------------------
# Domain entities
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Bet:
    bet_id: int
    creator_id: int
    target: str
    created_at: datetime
    status: str = "open"


@dataclass
class Entry:
    entry_id: int
    bet_id: int
    user_id: int
    period_key: str
    amount: int
    weight: int
    payout: int | None = None


# ---------------------------------------------------------------------------
# Decision objects (returned by domain services)
# ---------------------------------------------------------------------------

@dataclass
class JoinDecision:
    weight: int
    amount: int  # always 100
    balance_delta: int  # first_time: +400, repeat: -100
    first_time: bool


@dataclass
class SettleDecision:
    winners: list[str]
    payouts: dict[int, int]  # entry_id -> payout
    elapsed_sec: float
    k: float
