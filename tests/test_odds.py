"""odds モジュールのユニットテスト — 6 ケース。"""
from __future__ import annotations

import pytest
from odds import EntryInput, calc_payouts, find_winners, PERIOD_SECONDS, PERIOD_MULT


def make_entries(*args: tuple) -> list[EntryInput]:
    """ヘルパー: (entry_id, period_key, weight) タプルから EntryInput を生成。amount=100 固定。"""
    return [EntryInput(entry_id=eid, period_key=pk, amount=100, weight=w) for eid, pk, w in args]


# ケース 1: 単独勝者、k=1（経過時間 == 勝ち period ぴったり）
def test_single_winner_k1():
    elapsed = float(PERIOD_SECONDS["1w"])  # 604800秒
    entries = make_entries(
        (1, "1w", 64),
        (2, "1mo", 36),
    )
    total_pool = sum(e.amount for e in entries)  # 200
    alive = ["1w", "1mo", "3mo", "6mo", "1y"]
    winners = find_winners(elapsed, alive)
    assert winners == ["1w"]

    payouts = calc_payouts(entries, winners, elapsed, total_pool)
    # k=1, M=1.7, エントリ 1 が勝者グループの全額を取得
    # base_share_1 = 200 * (64*100) / (64*100) = 200
    # payout = round(1.0 * 1.7 * 200) = 340
    assert payouts[1] == 340
    assert payouts[2] == 0


# ケース 2: 単独勝者、k < 1（経過時間 != 勝ち period）
def test_single_winner_k_partial():
    # elapsed = 700000秒: "1w"(604800) に最も近い
    # dist_1w = |604800 - 700000| = 95200
    # dist_2w = |1209600 - 700000| = 509600  → "1w" が勝ち
    w_sec = PERIOD_SECONDS["1w"]  # 604800
    elapsed = 700_000.0
    k = min(w_sec, elapsed) / max(w_sec, elapsed)  # 604800/700000

    entries = make_entries(
        (1, "1w", 64),
        (2, "2w", 49),
    )
    total_pool = 200
    alive = ["1w", "2w", "1mo", "3mo", "6mo", "1y"]
    winners = find_winners(elapsed, alive)
    assert winners == ["1w"]

    payouts = calc_payouts(entries, winners, elapsed, total_pool)
    # base_share_1 = 200 * (64*100)/(64*100) = 200（勝者グループのみ）
    # payout = round(k * 1.7 * 200)
    assert payouts[1] == round(k * PERIOD_MULT["1w"] * 200)
    assert payouts[2] == 0


# ケース 3: 同着、両方のグループに賭けあり
def test_tie_both_groups():
    # "1w"(604800) と "2w"(1209600) の等距離
    # 中間点 = (604800 + 1209600) / 2 = 907200
    elapsed = (PERIOD_SECONDS["1w"] + PERIOD_SECONDS["2w"]) / 2.0

    entries = make_entries(
        (1, "1w", 64),
        (2, "2w", 64),
    )
    total_pool = 200
    alive = ["1w", "2w", "1mo", "3mo", "6mo", "1y"]
    winners = find_winners(elapsed, alive)
    assert set(winners) == {"1w", "2w"}

    payouts = calc_payouts(entries, winners, elapsed, total_pool)
    # 両グループとも配当あり（非ゼロ）
    assert payouts[1] > 0
    assert payouts[2] > 0


# ケース 4: 同着、片方のグループのみ賭けあり → そちらが全額取得
def test_tie_one_side_empty():
    elapsed = (PERIOD_SECONDS["1w"] + PERIOD_SECONDS["2w"]) / 2.0

    # "2w" にのみ賭けあり
    entries = make_entries(
        (1, "2w", 64),
    )
    total_pool = 100
    alive = ["1w", "2w", "1mo", "3mo", "6mo", "1y"]
    winners = find_winners(elapsed, alive)
    assert set(winners) == {"1w", "2w"}

    payouts = calc_payouts(entries, winners, elapsed, total_pool)
    # エントリ 1 ("2w") がプール全額を取得
    assert payouts[1] > 0


# ケース 5: 勝ち period に誰も賭けていない → 全員に賭け金返金
def test_no_bets_on_winner_returns_stake():
    elapsed = float(PERIOD_SECONDS["1w"])

    # "1w" に誰も賭けていない
    entries = make_entries(
        (1, "1mo", 36),
        (2, "3mo", 25),
    )
    total_pool = 200
    alive = ["1w", "1mo", "3mo", "6mo", "1y"]
    winners = find_winners(elapsed, alive)
    assert winners == ["1w"]

    payouts = calc_payouts(entries, winners, elapsed, total_pool)
    # 全員に賭け金が返金される
    assert payouts[1] == 100
    assert payouts[2] == 100


# ケース 6: period 倍率によりハウス赤字（配当合計 > プール）
def test_period_multiplier_house_deficit():
    elapsed = float(PERIOD_SECONDS["1y"])  # ちょうど 1 年、k=1

    entries = make_entries(
        (1, "1y", 64),
        (2, "1y", 64),
    )
    total_pool = 200
    alive = ["1y"]
    winners = find_winners(elapsed, alive)
    assert winners == ["1y"]

    payouts = calc_payouts(entries, winners, elapsed, total_pool)
    total_paid = sum(payouts.values())
    # M=15.0x, k=1 → 各 round(1.0 * 15.0 * 100) = 1500
    # 合計 = 3000, プール = 200 → ハウス赤字を確認
    assert total_paid > total_pool, f"配当合計 {total_paid} がプール {total_pool} 以下"
    assert payouts[1] == 1500
    assert payouts[2] == 1500
