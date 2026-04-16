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
    STAKE,
)
from domain.odds import PERIOD_KEYS, PERIOD_SECONDS


def _utc(year: int = 2025, month: int = 1, day: int = 1) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _make_bet(
    bet_id: int = 1,
    creator_id: int = 100,
    target: str = "テスト",
    created_at: datetime | None = None,
    status: str = "open",
    live_periods: list[str] | None = None,
) -> Bet:
    return Bet(
        bet_id=bet_id,
        creator_id=creator_id,
        target=target,
        created_at=created_at or _utc(),
        status=status,
        live_periods=list(live_periods) if live_periods is not None else list(PERIOD_KEYS),
    )


# ---------------------------------------------------------------------------
# Bet.place_bet
# ---------------------------------------------------------------------------

class TestPlaceBet:
    def test_first_time_gives_bonus(self):
        """初回参加時は +500 ボーナスが付与される（-100 賭け金 + 500 = +400）。"""
        bet = _make_bet()
        decision = bet.place_bet(user_id=200, period_key="1w")
        assert decision.first_time is True
        assert decision.balance_delta == 400
        assert decision.amount == STAKE
        assert decision.weight == len(PERIOD_KEYS) ** 2
        # エントリが追加されている
        assert len(bet.entries) == 1
        assert bet.entries[0].user_id == 200
        assert bet.entries[0].period_key == "1w"

    def test_repeat_join_no_bonus(self):
        """同一 bet への 2 回目以降はボーナスなし（-100 のみ）。"""
        bet = _make_bet()
        bet.place_bet(user_id=200, period_key="1w")
        decision = bet.place_bet(user_id=200, period_key="1mo")
        assert decision.first_time is False
        assert decision.balance_delta == -STAKE
        assert len(bet.entries) == 2

    def test_closed_bet_raises(self):
        """締め切り済みの bet に参加しようとすると BetAlreadyClosed。"""
        bet = _make_bet(status="closed")
        with pytest.raises(BetAlreadyClosed):
            bet.place_bet(user_id=200, period_key="1w")

    def test_eliminated_period_raises(self):
        """消滅済みの period を指定すると PeriodEliminated。"""
        bet = _make_bet(live_periods=["2w", "1mo", "3mo", "6mo", "1y"])
        with pytest.raises(PeriodEliminated):
            bet.place_bet(user_id=200, period_key="1w")

    def test_weight_depends_on_live_count(self):
        """weight は有効な period 数の 2 乗で決まる。"""
        bet5 = _make_bet(live_periods=list(PERIOD_KEYS)[:5])
        bet3 = _make_bet(live_periods=list(PERIOD_KEYS)[:3])
        d5 = bet5.place_bet(user_id=200, period_key=bet5.live_periods[0])
        d3 = bet3.place_bet(user_id=300, period_key=bet3.live_periods[0])
        assert d5.weight == 25  # 5^2
        assert d3.weight == 9   # 3^2


# ---------------------------------------------------------------------------
# Bet.eliminate_period
# ---------------------------------------------------------------------------

class TestEliminatePeriod:
    def test_removes_from_live_periods(self):
        """eliminate_period は live_periods から指定キーを取り除く。"""
        bet = _make_bet()
        assert "1d" in bet.live_periods
        bet.eliminate_period("1d")
        assert "1d" not in bet.live_periods

    def test_idempotent(self):
        """既に消滅済みの period を再消滅しても例外は出ない。"""
        bet = _make_bet()
        bet.eliminate_period("1d")
        bet.eliminate_period("1d")  # 2 回目は no-op
        assert "1d" not in bet.live_periods


# ---------------------------------------------------------------------------
# Bet.close
# ---------------------------------------------------------------------------

class TestClose:
    def test_basic_single_winner(self):
        """勝ち period に賭けた人が配当を得て、負けた人は 0。"""
        bet = _make_bet(
            created_at=_utc(),
            live_periods=["1w", "2w", "1mo", "3mo", "6mo", "1y"],
        )
        bet.place_bet(user_id=200, period_key="1w")
        bet.place_bet(user_id=300, period_key="1mo")

        # ちょうど 1 週間で締め → k=1, 勝ち="1w"
        now = _utc() + timedelta(seconds=PERIOD_SECONDS["1w"])
        result = bet.close(actor_user_id=100, now=now)

        assert result.winners == ["1w"]
        assert result.k == pytest.approx(1.0)
        assert bet.status == "closed"
        assert bet.entries[0].payout > 0   # 勝者
        assert bet.entries[1].payout == 0  # 敗者

    def test_all_same_period(self):
        """全員が同じ勝ち period に賭けた場合 — weight で按分。"""
        bet = _make_bet(
            created_at=_utc(),
            live_periods=["1w", "2w", "1mo", "3mo", "6mo", "1y"],
        )
        bet.place_bet(user_id=200, period_key="1w")
        bet.place_bet(user_id=300, period_key="1w")

        now = _utc() + timedelta(seconds=PERIOD_SECONDS["1w"])
        result = bet.close(actor_user_id=100, now=now)

        assert result.winners == ["1w"]
        # 同じタイミング・同じ live_periods → weight 同一 → 配当も同額
        assert bet.entries[0].payout == bet.entries[1].payout
        assert bet.entries[0].payout > 0

    def test_no_bets_on_winner_returns_stake(self):
        """勝ち period に誰も賭けていない場合 — 全員に賭け金を返金。"""
        bet = _make_bet(
            created_at=_utc(),
            live_periods=["1w", "1mo", "3mo", "6mo", "1y"],
        )
        bet.place_bet(user_id=200, period_key="1mo")
        bet.place_bet(user_id=300, period_key="3mo")

        now = _utc() + timedelta(seconds=PERIOD_SECONDS["1w"])
        result = bet.close(actor_user_id=100, now=now)

        assert result.winners == ["1w"]
        assert bet.entries[0].payout == STAKE
        assert bet.entries[1].payout == STAKE

    def test_not_creator_raises(self):
        """作成者以外が締めようとすると NotAllowed。"""
        bet = _make_bet(creator_id=100)
        with pytest.raises(NotAllowed):
            bet.close(actor_user_id=999, now=_utc())

    def test_closed_bet_raises(self):
        """既に締め切り済みの bet を再度締めると BetAlreadyClosed。"""
        bet = _make_bet(status="closed")
        with pytest.raises(BetAlreadyClosed):
            bet.close(actor_user_id=100, now=_utc())

    def test_no_entries(self):
        """参加者ゼロで締めた場合 — 配当は空。"""
        bet = _make_bet(created_at=_utc())
        now = _utc() + timedelta(days=1)
        result = bet.close(actor_user_id=100, now=now)
        assert result.payouts == {}
        assert bet.status == "closed"

    def test_k_factor_less_than_one(self):
        """経過時間が period のマイルストーンと一致しない場合、k < 1。"""
        bet = _make_bet(
            created_at=_utc(),
            live_periods=["1w", "2w", "1mo", "3mo", "6mo", "1y"],
        )
        bet.place_bet(user_id=200, period_key="1w")

        # 700,000秒で締め（1w=604800 に近いが一致しない）
        now = _utc() + timedelta(seconds=700_000)
        result = bet.close(actor_user_id=100, now=now)

        assert result.winners == ["1w"]
        assert result.k < 1.0
        assert result.k == pytest.approx(PERIOD_SECONDS["1w"] / 700_000)


# ---------------------------------------------------------------------------
# Full lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_create_join_settle(self):
        """賭け作成 → 2人参加 → 決済の一連フロー。"""
        bet = _make_bet(created_at=_utc())

        # ユーザー A が "1w" に参加
        d1 = bet.place_bet(user_id=200, period_key="1w")
        assert d1.first_time is True

        # ユーザー B が "1mo" に参加
        d2 = bet.place_bet(user_id=300, period_key="1mo")
        assert d2.first_time is True

        # ユーザー A が "1w" に再参加（ボーナスなし）
        d3 = bet.place_bet(user_id=200, period_key="1w")
        assert d3.first_time is False
        assert d3.balance_delta == -STAKE

        # ちょうど 1 週間で決済 → "1w" が勝ち
        now = _utc() + timedelta(seconds=PERIOD_SECONDS["1w"])
        result = bet.close(actor_user_id=100, now=now)

        assert result.winners == ["1w"]
        assert result.k == pytest.approx(1.0)
        # ユーザー A の 2 エントリ両方に配当
        assert bet.entries[0].payout > 0  # A の 1 件目 (1w)
        assert bet.entries[2].payout > 0  # A の 2 件目 (1w)
        # ユーザー B (1mo) は敗北
        assert bet.entries[1].payout == 0

    def test_lifecycle_with_period_elimination(self):
        """期間消滅により live_periods が減る状況をシミュレート。"""
        bet = _make_bet(created_at=_utc())

        # ユーザー A が早期参加（全 8 期間有効 → 高 weight）
        d1 = bet.place_bet(user_id=200, period_key="3mo")
        assert d1.weight == 64  # 8^2

        # 1d, 3d が経過し、消滅
        bet.eliminate_period("1d")
        bet.eliminate_period("3d")
        assert len(bet.live_periods) == 6

        # ユーザー B が後から参加（6 期間有効 → 低 weight）
        d2 = bet.place_bet(user_id=300, period_key="3mo")
        assert d2.weight == 36  # 6^2

        # 3 か月で決済
        now = _utc() + timedelta(seconds=PERIOD_SECONDS["3mo"])
        result = bet.close(actor_user_id=100, now=now)

        assert result.winners == ["3mo"]
        # ユーザー A は早期参加 → 高 weight → 高配当
        assert bet.entries[0].payout > bet.entries[1].payout
