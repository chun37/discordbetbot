"""ドメインロジックのテスト — DB・Discord・async 不要。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from domain.models import (
    Bet,
    BetAlreadyClosed,
    Entry,
    NotAllowed,
    PeriodEliminated,
)
from domain.odds import PERIOD_KEYS, PERIOD_SECONDS, PERIOD_MULT
from domain.services import STAKE, validate_join, settle


def _utc(year: int = 2025, month: int = 1, day: int = 1) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _make_bet(
    bet_id: int = 1,
    creator_id: int = 100,
    target: str = "テスト",
    created_at: datetime | None = None,
    status: str = "open",
) -> Bet:
    return Bet(
        bet_id=bet_id,
        creator_id=creator_id,
        target=target,
        created_at=created_at or _utc(),
        status=status,
    )


class _EntryFactory:
    """entry_id を自動採番するファクトリ。"""

    def __init__(self, bet_id: int = 1) -> None:
        self.bet_id = bet_id
        self._next_id = 1

    def create(self, user_id: int, period_key: str, weight: int) -> Entry:
        entry = Entry(
            entry_id=self._next_id,
            bet_id=self.bet_id,
            user_id=user_id,
            period_key=period_key,
            amount=STAKE,
            weight=weight,
        )
        self._next_id += 1
        return entry


# ---------------------------------------------------------------------------
# validate_join
# ---------------------------------------------------------------------------

class TestValidateJoin:
    def test_first_time_gives_bonus(self):
        bet = _make_bet()
        decision = validate_join(bet, [], list(PERIOD_KEYS), user_id=200, period_key="1w")
        assert decision.first_time is True
        assert decision.balance_delta == 400  # +500 bonus - 100 stake
        assert decision.amount == 100
        assert decision.weight == len(PERIOD_KEYS) ** 2

    def test_repeat_join_no_bonus(self):
        bet = _make_bet()
        existing = [Entry(1, 1, 200, "1w", 100, 64)]
        decision = validate_join(bet, existing, list(PERIOD_KEYS), user_id=200, period_key="1mo")
        assert decision.first_time is False
        assert decision.balance_delta == -100

    def test_closed_bet_raises(self):
        bet = _make_bet(status="closed")
        with pytest.raises(BetAlreadyClosed):
            validate_join(bet, [], list(PERIOD_KEYS), user_id=200, period_key="1w")

    def test_eliminated_period_raises(self):
        bet = _make_bet()
        live = ["2w", "1mo", "3mo", "6mo", "1y"]  # "1d","3d","1w" eliminated
        with pytest.raises(PeriodEliminated):
            validate_join(bet, [], live, user_id=200, period_key="1w")

    def test_weight_depends_on_live_count(self):
        bet = _make_bet()
        live_5 = list(PERIOD_KEYS)[:5]
        live_3 = list(PERIOD_KEYS)[:3]
        d5 = validate_join(bet, [], live_5, user_id=200, period_key=live_5[0])
        d3 = validate_join(bet, [], live_3, user_id=300, period_key=live_3[0])
        assert d5.weight == 25  # 5^2
        assert d3.weight == 9   # 3^2


# ---------------------------------------------------------------------------
# settle
# ---------------------------------------------------------------------------

class TestSettle:
    def test_basic_single_winner(self):
        """勝ち period に賭けた人が配当を得て、負けた人は 0。"""
        bet = _make_bet(created_at=_utc())
        f = _EntryFactory()
        entries = [
            f.create(user_id=200, period_key="1w", weight=64),
            f.create(user_id=300, period_key="1mo", weight=64),
        ]
        live = ["1w", "2w", "1mo", "3mo", "6mo", "1y"]

        # ちょうど 1 週間で締め → k=1, 勝ち="1w"
        now = _utc() + timedelta(seconds=PERIOD_SECONDS["1w"])
        result = settle(bet, entries, live, actor_user_id=100, now=now)

        assert result.winners == ["1w"]
        assert result.k == pytest.approx(1.0)
        assert result.payouts[1] > 0   # 勝者
        assert result.payouts[2] == 0  # 敗者

    def test_all_same_period(self):
        """全員が同じ勝ち period に賭けた場合 — weight で按分。"""
        bet = _make_bet(created_at=_utc())
        f = _EntryFactory()
        entries = [
            f.create(user_id=200, period_key="1w", weight=64),
            f.create(user_id=300, period_key="1w", weight=64),
        ]
        live = ["1w", "2w", "1mo", "3mo", "6mo", "1y"]

        now = _utc() + timedelta(seconds=PERIOD_SECONDS["1w"])
        result = settle(bet, entries, live, actor_user_id=100, now=now)

        assert result.winners == ["1w"]
        # weight 同一 → 配当も同額
        assert result.payouts[1] == result.payouts[2]
        assert result.payouts[1] > 0

    def test_no_bets_on_winner_returns_stake(self):
        """勝ち period に誰も賭けていない場合 — 全員に賭け金を返金。"""
        bet = _make_bet(created_at=_utc())
        f = _EntryFactory()
        entries = [
            f.create(user_id=200, period_key="1mo", weight=36),
            f.create(user_id=300, period_key="3mo", weight=25),
        ]
        live = ["1w", "1mo", "3mo", "6mo", "1y"]

        now = _utc() + timedelta(seconds=PERIOD_SECONDS["1w"])
        result = settle(bet, entries, live, actor_user_id=100, now=now)

        assert result.winners == ["1w"]
        assert result.payouts[1] == STAKE
        assert result.payouts[2] == STAKE

    def test_not_creator_raises(self):
        bet = _make_bet(creator_id=100)
        with pytest.raises(NotAllowed):
            settle(bet, [], list(PERIOD_KEYS), actor_user_id=999, now=_utc())

    def test_closed_bet_raises(self):
        bet = _make_bet(status="closed")
        with pytest.raises(BetAlreadyClosed):
            settle(bet, [], list(PERIOD_KEYS), actor_user_id=100, now=_utc())

    def test_no_entries(self):
        """参加者ゼロで締めた場合 — 配当は空。"""
        bet = _make_bet(created_at=_utc())
        now = _utc() + timedelta(days=1)
        result = settle(bet, [], list(PERIOD_KEYS), actor_user_id=100, now=now)
        assert result.payouts == {}

    def test_k_factor_less_than_one(self):
        """経過時間が period のマイルストーンと一致しない場合、k < 1。"""
        bet = _make_bet(created_at=_utc())
        f = _EntryFactory()
        entries = [
            f.create(user_id=200, period_key="1w", weight=64),
        ]
        live = ["1w", "2w", "1mo", "3mo", "6mo", "1y"]

        # 700,000秒で締め（1w=604800 に近いが一致しない）
        now = _utc() + timedelta(seconds=700_000)
        result = settle(bet, entries, live, actor_user_id=100, now=now)

        assert result.winners == ["1w"]
        assert result.k < 1.0
        assert result.k == pytest.approx(
            PERIOD_SECONDS["1w"] / 700_000
        )


# ---------------------------------------------------------------------------
# Full lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_create_join_settle(self):
        """賭け作成 → 2人参加 → 決済の一連フロー。"""
        bet = _make_bet(created_at=_utc())
        f = _EntryFactory()
        all_periods = list(PERIOD_KEYS)
        entries: list[Entry] = []

        # ユーザー A が "1w" に参加
        d1 = validate_join(bet, entries, all_periods, user_id=200, period_key="1w")
        assert d1.first_time is True
        entries.append(f.create(200, "1w", d1.weight))

        # ユーザー B が "1mo" に参加
        d2 = validate_join(bet, entries, all_periods, user_id=300, period_key="1mo")
        assert d2.first_time is True
        entries.append(f.create(300, "1mo", d2.weight))

        # ユーザー A が "1w" に再参加（ボーナスなし）
        d3 = validate_join(bet, entries, all_periods, user_id=200, period_key="1w")
        assert d3.first_time is False
        assert d3.balance_delta == -100
        entries.append(f.create(200, "1w", d3.weight))

        # ちょうど 1 週間で決済 → "1w" が勝ち
        now = _utc() + timedelta(seconds=PERIOD_SECONDS["1w"])
        result = settle(bet, entries, all_periods, actor_user_id=100, now=now)

        assert result.winners == ["1w"]
        assert result.k == pytest.approx(1.0)
        # ユーザー A の 2 エントリ両方に配当
        assert result.payouts[1] > 0  # エントリ 1 (A, 1w)
        assert result.payouts[3] > 0  # エントリ 3 (A, 1w)
        # ユーザー B (1mo) は敗北
        assert result.payouts[2] == 0

    def test_lifecycle_with_period_elimination(self):
        """期間消滅により live_periods が減る状況をシミュレート。"""
        bet = _make_bet(created_at=_utc())
        f = _EntryFactory()
        entries: list[Entry] = []

        # 初期は全期間が有効
        all_periods = list(PERIOD_KEYS)

        # ユーザー A が早期参加（全 8 期間有効 → 高 weight）
        d1 = validate_join(bet, entries, all_periods, user_id=200, period_key="3mo")
        assert d1.weight == 64  # 8^2
        entries.append(f.create(200, "3mo", d1.weight))

        # 1d, 3d が経過し、残り 6 期間
        later_live = ["1w", "2w", "1mo", "3mo", "6mo", "1y"]

        # ユーザー B が後から参加（6 期間有効 → 低 weight）
        d2 = validate_join(bet, entries, later_live, user_id=300, period_key="3mo")
        assert d2.weight == 36  # 6^2
        entries.append(f.create(300, "3mo", d2.weight))

        # 3 か月で決済
        now = _utc() + timedelta(seconds=PERIOD_SECONDS["3mo"])
        result = settle(bet, entries, later_live, actor_user_id=100, now=now)

        assert result.winners == ["3mo"]
        # ユーザー A は早期参加 → 高 weight → 高配当
        assert result.payouts[1] > result.payouts[2]
