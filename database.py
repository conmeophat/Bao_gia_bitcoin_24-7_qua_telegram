from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo


VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def _format_price(value: str | Decimal) -> str:
    rendered = format(Decimal(str(value)), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    whole, dot, fraction = rendered.partition(".")
    whole = f"{int(whole):,}"
    return whole + (dot + fraction if fraction else "")


class Database:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 20000")
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(VN_TZ).isoformat(timespec="seconds")

    def initialize(self) -> None:
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    direction TEXT NOT NULL CHECK(direction IN ('below', 'above')),
                    threshold TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    triggered INTEGER NOT NULL DEFAULT 0,
                    trigger_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS prices (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    price TEXT NOT NULL,
                    ts_ms INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    received_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_id INTEGER NOT NULL,
                    trigger_no INTEGER NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    sent_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    UNIQUE(alert_id, trigger_no),
                    FOREIGN KEY(alert_id) REFERENCES alerts(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_outbox_pending
                    ON outbox(sent_at, id);
                CREATE INDEX IF NOT EXISTS idx_alerts_active
                    ON alerts(active, id);
                """
            )

    def create_alert(self, direction: str, threshold: Decimal) -> dict:
        if direction not in {"below", "above"}:
            raise ValueError("Unsupported alert direction.")
        now = self._now()
        with self.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO alerts(direction, threshold, active, triggered,
                                   trigger_count, created_at, updated_at)
                VALUES (?, ?, 1, 0, 0, ?, ?)
                """,
                (direction, str(threshold), now, now),
            )
            row = conn.execute(
                "SELECT * FROM alerts WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            return dict(row)

    def list_alerts(self, active_only: bool = False) -> list[dict]:
        query = "SELECT * FROM alerts"
        if active_only:
            query += " WHERE active = 1"
        query += " ORDER BY id"
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(query).fetchall()]

    def get_alert(self, alert_id: int) -> dict | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM alerts WHERE id = ?", (alert_id,)
            ).fetchone()
            return dict(row) if row else None

    def set_alert_active(self, alert_id: int, active: bool) -> bool:
        with self.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE alerts
                SET active = ?, triggered = CASE WHEN ? = 1 THEN 0 ELSE triggered END,
                    updated_at = ?
                WHERE id = ?
                """,
                (1 if active else 0, 1 if active else 0, self._now(), alert_id),
            )
            return cursor.rowcount > 0

    def delete_alert(self, alert_id: int) -> bool:
        with self.connection() as conn:
            cursor = conn.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))
            return cursor.rowcount > 0

    def count_active_alerts(self) -> int:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM alerts WHERE active = 1"
            ).fetchone()
            return int(row["count"])

    def last_price(self) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM prices WHERE id = 1").fetchone()
            return dict(row) if row else None

    def record_and_evaluate(
        self, price: Decimal, ts_ms: int, source: str
    ) -> bool:
        """Persist a fresh ticker and atomically create outbox events for new crossings."""
        if not price.is_finite() or price <= 0 or ts_ms <= 0:
            return False

        now = self._now()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                "SELECT ts_ms FROM prices WHERE id = 1"
            ).fetchone()
            if previous and ts_ms < int(previous["ts_ms"]):
                conn.rollback()
                return False

            conn.execute(
                """
                INSERT INTO prices(id, price, ts_ms, source, received_at)
                VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    price = excluded.price,
                    ts_ms = excluded.ts_ms,
                    source = excluded.source,
                    received_at = excluded.received_at
                """,
                (str(price), ts_ms, source, now),
            )

            alerts = conn.execute(
                "SELECT * FROM alerts WHERE active = 1 ORDER BY id"
            ).fetchall()

            for alert in alerts:
                threshold = Decimal(alert["threshold"])
                direction = alert["direction"]
                triggered = bool(alert["triggered"])
                is_met = (
                    price <= threshold if direction == "below" else price >= threshold
                )

                if is_met and not triggered:
                    trigger_no = int(alert["trigger_count"]) + 1
                    comparator = "≤" if direction == "below" else "≥"
                    local_time = datetime.fromtimestamp(
                        ts_ms / 1000, tz=VN_TZ
                    ).strftime("%d/%m/%Y %H:%M:%S.%f")[:-3]
                    condition = (
                        f"BTC-USDT {comparator} {_format_price(threshold)} USDT"
                    )
                    message = (
                        "🚨 CẢNH BÁO GIÁ BITCOIN\n"
                        "Sàn: OKX | Thị trường: Spot\n"
                        "Cặp: BTC-USDT\n"
                        f"Điều kiện: {condition}\n"
                        f"Giá khớp cuối (last): {_format_price(price)} USDT\n"
                        f"Thời điểm dữ liệu OKX: {local_time} (UTC+7)\n"
                        "Nguồn: OKX public ticker\n"
                        f"Mã cảnh báo: #{alert['id']}-{trigger_no}"
                    )
                    conn.execute(
                        """
                        UPDATE alerts
                        SET triggered = 1, trigger_count = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (trigger_no, now, alert["id"]),
                    )
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO outbox(
                            alert_id, trigger_no, message, created_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (alert["id"], trigger_no, message, now),
                    )
                elif not is_met and triggered:
                    # Re-arm only after price has moved back outside the condition.
                    conn.execute(
                        "UPDATE alerts SET triggered = 0, updated_at = ? WHERE id = ?",
                        (now, alert["id"]),
                    )

            conn.commit()
        return True

    def pending_notifications(self, limit: int = 20) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, alert_id, trigger_no, message, attempts
                FROM outbox
                WHERE sent_at IS NULL
                ORDER BY id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def mark_notification_sent(self, notification_id: int) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE outbox
                SET sent_at = ?, attempts = attempts + 1, last_error = NULL
                WHERE id = ?
                """,
                (self._now(), notification_id),
            )

    def mark_notification_failed(self, notification_id: int, error: str) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE outbox
                SET attempts = attempts + 1, last_error = ?
                WHERE id = ?
                """,
                (error[:1000], notification_id),
            )

    def get_setting(self, key: str, default: str = "") -> str:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
            return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
