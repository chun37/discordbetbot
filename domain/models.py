from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from domain.odds import (
    EntryInput,
    PERIOD_KEYS,
    PERIOD_SECONDS,
    calc_payouts,
    calc_weight,
    find_winners,
)


STAKE = 100
FIRST_TIME_BONUS = 500


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class BetError(Exception):
    """ドメイン例外の基底クラス。"""


class BetNotFound(BetError):
    pass


class BetAlreadyClosed(BetError):
    pass


class NotAllowed(BetError):
    pass


class PeriodEliminated(BetError):
    pass


# ---------------------------------------------------------------------------
# Decision objects (返り値)
# ---------------------------------------------------------------------------

@dataclass
class JoinDecision:
    weight: int
    amount: int           # 常に 100
    balance_delta: int    # first_time: +400, repeat: -100
    first_time: bool


@dataclass
class SettleDecision:
    winners: list[str]
    payouts: dict[int, int]   # entry_id -> payout
    elapsed_sec: float
    k: float


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------

@dataclass
class Entry:
    entry_id: int
    bet_id: int
    user_id: int
    period_key: str
    amount: int
    weight: int
    payout: int | None = None


@dataclass
class Bet:
    """賭けアグリゲート。entries と live_periods を保持し、ライフサイクル操作を提供。"""

    bet_id: int
    creator_id: int
    target: str
    created_at: datetime
    status: str = "open"
    entries: list[Entry] = field(default_factory=list)
    live_periods: list[str] = field(default_factory=lambda: list(PERIOD_KEYS))

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------

    def place_bet(self, user_id: int, period_key: str) -> JoinDecision:
        """参加リクエストをバリデートしてエントリを追加、判定結果を返す。

        entry_id は既存最大 + 1 で自動採番（アプリ層では DB が別途採番するため
        採番値は使わず、返り値の weight / balance_delta のみ利用する）。
        """
        if self.status != "open":
            raise BetAlreadyClosed(f"Bet #{self.bet_id} is already closed")
        if period_key not in self.live_periods:
            raise PeriodEliminated(f"Period {period_key} has already been eliminated")

        first_time = all(e.user_id != user_id for e in self.entries)
        weight = calc_weight(len(self.live_periods))
        delta = (FIRST_TIME_BONUS - STAKE) if first_time else -STAKE

        next_id = max((e.entry_id for e in self.entries), default=0) + 1
        self.entries.append(
            Entry(
                entry_id=next_id,
                bet_id=self.bet_id,
                user_id=user_id,
                period_key=period_key,
                amount=STAKE,
                weight=weight,
            )
        )

        return JoinDecision(
            weight=weight,
            amount=STAKE,
            balance_delta=delta,
            first_time=first_time,
        )

    def eliminate_period(self, period_key: str) -> None:
        """指定 period を消滅扱いにする（マイルストーン経過）。"""
        if period_key in self.live_periods:
            self.live_periods.remove(period_key)

    def close(self, actor_user_id: int, now: datetime) -> SettleDecision:
        """締め切って配当を計算し、各エントリの payout を確定する。"""
        if self.status != "open":
            raise BetAlreadyClosed(f"Bet #{self.bet_id} is already closed")
        if self.creator_id != actor_user_id:
            raise NotAllowed("Only the creator can close this bet")

        elapsed_sec = max(0.0, (now - self.created_at).total_seconds())
        winners = find_winners(elapsed_sec, self.live_periods)
        total_pool = len(self.entries) * STAKE

        entry_inputs = [
            EntryInput(
                entry_id=e.entry_id,
                period_key=e.period_key,
                amount=e.amount,
                weight=e.weight,
            )
            for e in self.entries
        ]
        payouts = calc_payouts(entry_inputs, winners, elapsed_sec, total_pool)

        if winners:
            w_sec = PERIOD_SECONDS[winners[0]]
            denom = max(w_sec, elapsed_sec)
            k = min(w_sec, elapsed_sec) / denom if denom > 0 else 0.0
        else:
            k = 0.0

        # 各 entry に payout を反映
        for entry in self.entries:
            entry.payout = payouts.get(entry.entry_id, 0)

        self.status = "closed"

        return SettleDecision(
            winners=winners,
            payouts=payouts,
            elapsed_sec=elapsed_sec,
            k=k,
        )
