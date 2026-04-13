from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    balance INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS bets (
    bet_id INTEGER PRIMARY KEY AUTOINCREMENT,
    creator_id INTEGER NOT NULL,
    target TEXT NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    closed_at TEXT,
    elapsed_seconds INTEGER,
    winning_periods TEXT
);

CREATE INDEX IF NOT EXISTS idx_bets_status ON bets(status);

CREATE TABLE IF NOT EXISTS entries (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    bet_id INTEGER NOT NULL REFERENCES bets(bet_id),
    user_id INTEGER NOT NULL,
    period_key TEXT NOT NULL,
    amount INTEGER NOT NULL,
    weight INTEGER NOT NULL,
    payout INTEGER,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entries_bet ON entries(bet_id);
CREATE INDEX IF NOT EXISTS idx_entries_user ON entries(user_id);

CREATE TABLE IF NOT EXISTS schedules (
    schedule_id INTEGER PRIMARY KEY AUTOINCREMENT,
    bet_id INTEGER NOT NULL REFERENCES bets(bet_id),
    period_key TEXT NOT NULL,
    fire_at TEXT NOT NULL,
    fired INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_schedules_pending ON schedules(fired, fire_at);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.commit()
        await self._init_schema()
        logger.info("Database connected: %s", self._path)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database.connect() has not been called"
        return self._conn

    async def _init_schema(self) -> None:
        await self.conn.executescript(_SCHEMA)
        await self.conn.commit()
        async with self.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            await self.conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', '1')"
            )
            await self.conn.commit()
            logger.info("Schema initialized at version 1")
        else:
            logger.info("Schema version: %s", row["value"])

    # ------------------------------------------------------------------
    # Transaction helper
    # ------------------------------------------------------------------

    async def execute_write(self, fn):
        """Run fn(conn) inside a BEGIN IMMEDIATE transaction, serialized by a lock."""
        async with self._write_lock:
            async with self.conn.execute("BEGIN IMMEDIATE"):
                pass
            try:
                result = await fn(self.conn)
                await self.conn.commit()
                return result
            except Exception:
                await self.conn.rollback()
                raise

    # ------------------------------------------------------------------
    # Bets
    # ------------------------------------------------------------------

    async def create_bet(
        self,
        creator_id: int,
        target: str,
        channel_id: int,
        created_at: str,
    ) -> int:
        async with self.conn.execute(
            """
            INSERT INTO bets(creator_id, target, channel_id, message_id, created_at)
            VALUES(?, ?, ?, 0, ?)
            """,
            (creator_id, target, channel_id, created_at),
        ) as cur:
            bet_id = cur.lastrowid
        await self.conn.commit()
        return bet_id

    async def update_bet_message_id(self, bet_id: int, message_id: int) -> None:
        await self.conn.execute(
            "UPDATE bets SET message_id=? WHERE bet_id=?",
            (message_id, bet_id),
        )
        await self.conn.commit()

    async def fetch_bet(self, bet_id: int) -> aiosqlite.Row | None:
        async with self.conn.execute(
            "SELECT * FROM bets WHERE bet_id=?", (bet_id,)
        ) as cur:
            return await cur.fetchone()

    async def fetch_open_bets(self) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            "SELECT * FROM bets WHERE status='open' ORDER BY bet_id"
        ) as cur:
            return await cur.fetchall()

    async def close_bet_tx(
        self,
        conn: aiosqlite.Connection,
        bet_id: int,
        closed_at: str,
        elapsed_seconds: int,
        winning_periods: list[str],
    ) -> None:
        await conn.execute(
            """
            UPDATE bets
            SET status='closed', closed_at=?, elapsed_seconds=?, winning_periods=?
            WHERE bet_id=?
            """,
            (closed_at, elapsed_seconds, json.dumps(winning_periods), bet_id),
        )

    # ------------------------------------------------------------------
    # Entries
    # ------------------------------------------------------------------

    async def insert_entry_tx(
        self,
        conn: aiosqlite.Connection,
        bet_id: int,
        user_id: int,
        period_key: str,
        amount: int,
        weight: int,
        created_at: str,
    ) -> int:
        async with conn.execute(
            """
            INSERT INTO entries(bet_id, user_id, period_key, amount, weight, created_at)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (bet_id, user_id, period_key, amount, weight, created_at),
        ) as cur:
            return cur.lastrowid

    async def update_entry_payout_tx(
        self,
        conn: aiosqlite.Connection,
        entry_id: int,
        payout: int,
    ) -> None:
        await conn.execute(
            "UPDATE entries SET payout=? WHERE entry_id=?",
            (payout, entry_id),
        )

    async def fetch_bet_entries(self, bet_id: int) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            "SELECT * FROM entries WHERE bet_id=? ORDER BY entry_id",
            (bet_id,),
        ) as cur:
            return await cur.fetchall()

    # ------------------------------------------------------------------
    # Users / balance
    # ------------------------------------------------------------------

    async def upsert_balance_tx(
        self,
        conn: aiosqlite.Connection,
        user_id: int,
        delta: int,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO users(user_id, balance) VALUES(?, ?)
            ON CONFLICT(user_id) DO UPDATE SET balance = balance + excluded.balance
            """,
            (user_id, delta),
        )

    async def fetch_balance(self, user_id: int) -> int:
        async with self.conn.execute(
            "SELECT balance FROM users WHERE user_id=?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        return row["balance"] if row else 0

    async def top_balances(self, limit: int = 10, offset: int = 0) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            "SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ) as cur:
            return await cur.fetchall()

    async def count_users(self) -> int:
        async with self.conn.execute("SELECT COUNT(*) FROM users") as cur:
            row = await cur.fetchone()
        return row[0]

    # ------------------------------------------------------------------
    # Schedules
    # ------------------------------------------------------------------

    async def insert_schedules(
        self,
        bet_id: int,
        entries: list[tuple[str, str]],  # [(period_key, fire_at_iso), ...]
    ) -> list[int]:
        ids = []
        for period_key, fire_at in entries:
            async with self.conn.execute(
                "INSERT INTO schedules(bet_id, period_key, fire_at) VALUES(?, ?, ?)",
                (bet_id, period_key, fire_at),
            ) as cur:
                ids.append(cur.lastrowid)
        await self.conn.commit()
        return ids

    async def fetch_pending_schedules(self) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            "SELECT * FROM schedules WHERE fired=0 ORDER BY fire_at"
        ) as cur:
            return await cur.fetchall()

    async def fetch_live_periods_tx(
        self,
        conn: aiosqlite.Connection,
        bet_id: int,
    ) -> list[str]:
        """Return period_keys of schedules that have not yet fired for this bet."""
        async with conn.execute(
            "SELECT period_key FROM schedules WHERE bet_id=? AND fired=0",
            (bet_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [r["period_key"] for r in rows]

    async def claim_schedule_success(self, schedule_id: int) -> None:
        await self.conn.execute(
            "UPDATE schedules SET fired=1 WHERE schedule_id=?",
            (schedule_id,),
        )
        await self.conn.commit()

    async def mark_schedules_fired_for_bet_tx(
        self,
        conn: aiosqlite.Connection,
        bet_id: int,
    ) -> None:
        await conn.execute(
            "UPDATE schedules SET fired=1 WHERE bet_id=?",
            (bet_id,),
        )
