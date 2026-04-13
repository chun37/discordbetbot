"""
残高推移グラフ生成モジュール。
matplotlib は起動時ではなく初回呼び出し時にロードされる（lazy import）。
"""
from __future__ import annotations

import io
from datetime import datetime


def generate_balance_history_png(
    events: list[tuple[datetime, int]],
    user_label: str,
) -> bytes:
    """
    events: [(ts, delta), ...] 時系列ソート済み
    user_label: グラフタイトル用ラベル（日本語 tofu 回避のため ASCII 推奨）
    戻り値: PNG bytes
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if not events:
        fig, ax = plt.subplots(figsize=(8, 4), dpi=100)
        ax.text(0.5, 0.5, "No data", ha="center", va="center", transform=ax.transAxes)
        ax.set_title(f"Balance History — {user_label}")
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
        plt.close(fig)
        plt.close("all")
        buf.seek(0)
        return buf.getvalue()

    xs = [e[0] for e in events]
    ys: list[int] = []
    acc = 0
    for _, delta in events:
        acc += delta
        ys.append(acc)

    fig, ax = plt.subplots(figsize=(8, 4), dpi=100)
    ax.step(xs, ys, where="post", linewidth=1.5)
    ax.fill_between(xs, ys, step="post", alpha=0.15)
    ax.set_title(f"Balance History — {user_label}")
    ax.set_xlabel("Time")
    ax.set_ylabel("Balance (P)")
    ax.grid(True, alpha=0.3)
    fig.autofmt_xdate()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    plt.close("all")
    buf.seek(0)
    return buf.getvalue()
