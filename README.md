# Discord Bet Bot

Discord 上で「いつ飽きるか」などの*期間予想*を賭けるゲーム BOT。

## セットアップ

依存管理は [uv](https://docs.astral.sh/uv/) を使用。Python >=3.12。

```bash
# 1. 依存インストール（.venv を自動生成）
uv sync

# 2. .env 作成
cp .env.example .env
# DISCORD_TOKEN と DEV_GUILD_ID を設定する

# 3. テスト実行
uv run pytest

# 4. BOT 起動
uv run python bot.py
```

`requirements.txt` / `requirements-dev.txt` は pip 用に残してあります。

## Discord Developer Portal の設定

1. https://discord.com/developers/applications → New Application
2. Bot タブ → Reset Token → .env に貼り付け
3. Bot の必要な権限:
   - `Send Messages`
   - `Embed Links`
   - `Use Application Commands`
4. OAuth2 → URL Generator: scopes `bot` + `applications.commands`
5. 生成した URL でサーバーに招待

## スラッシュコマンド

| コマンド | 説明 |
|---|---|
| `/bet-create target:<text>` | 新しい賭けを作成 |
| `/bet-list` | 進行中の賭け一覧 |
| `/balance [user]` | 残高・参加中の賭けを確認 |
| `/bet-history [user]` | 賭け履歴と残高推移グラフ |
| `/ranking` | 残高ランキング（10 名/ページ） |
| `/help` | ヘルプ表示 |

## 賭けの流れ

1. `/bet-create target:テストゲームをいつ飽きる？` でEmbedを投稿
2. **[参加する]** を押して期間を選択（100P消費、500Pボーナス付与）
3. 各期間が経過するとチャンネルに自動通知
4. 作成者が **[飽きた]** を押すと締め切り・配当計算・結果発表

## タイマーのテスト（期間短縮）

```bash
# 例: 全期間を数十秒に圧縮
PERIOD_SECONDS_OVERRIDE='{"1d":10,"3d":20,"1w":30,"2w":40,"1mo":50,"3mo":60,"6mo":70,"1y":80}' uv run python bot.py
```

## DB リセット

```bash
uv run python scripts/reset_db.py
```

## アーキテクチャ

ドメイン / DB / UI の 3 層に分離。ドメイン層は純粋関数のみで構成し、DB も Discord も使わず単体テスト可能。

```
bot.py                エントリポイント
config.py             .env 読み込み

# ── ドメイン層（純粋ロジック、I/O なし） ─────────────────
domain/
  models.py           Bet（アグリゲート）/ Entry / 例外 / Decision 型
  odds.py             配当計算（純関数）

# ── DB 層 ──────────────────────────────────────────────
db.py                 aiosqlite、スキーマ、クエリ

# ── UI / オーケストレーション層 ──────────────────────────
bet_service.py        Bet アグリゲートを DB から構築 → メソッド呼び出し → DB + Discord に反映
scheduler.py          期間経過通知タスク
embeds.py             Embed 構築
embed_refresher.py    デバウンス更新 (5 edits/5s 対策)
views/                DynamicItem ボタン・Select
cogs/                 スラッシュコマンド

# ── テスト ────────────────────────────────────────────
tests/test_domain.py  Bet ライフサイクルのテスト
tests/test_odds.py    配当計算のテスト

odds.py               domain/odds からの後方互換 re-export
```

### ドメインロジックのテスト

`Bet` がアグリゲートルートとして参加・消滅・締め切りの各操作をメソッドで提供。
DB や Discord なしで一連の流れを検証できます。

```python
from domain.models import Bet

bet = Bet(bet_id=1, creator_id=100, target="test", created_at=now)

# 参加
bet.place_bet(user_id=200, period_key="1w")
bet.place_bet(user_id=300, period_key="1mo")

# 期間消滅（マイルストーン経過のシミュレート）
bet.eliminate_period("1d")

# 締め切り・配当計算
result = bet.close(actor_user_id=100, now=close_time)
# → result.winners, result.payouts, result.k
# → bet.entries[i].payout にも反映済み
```
