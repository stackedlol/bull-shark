import sqlite3
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

from src.config import DB_PATH


class StateDB:
    def __init__(self, db_path: Path = DB_PATH):
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS product_state (
                product_id      TEXT PRIMARY KEY,
                anchor_price    TEXT,
                avg_entry_price TEXT,
                last_tp_band    INTEGER DEFAULT 0,
                last_tp_timestamp REAL DEFAULT 0,
                daily_trade_count INTEGER DEFAULT 0,
                daily_trade_date  TEXT,
                rebuy_order_id    TEXT,
                rebuy_price       TEXT,
                rebuy_size        TEXT,
                rebuy_placed_at   REAL DEFAULT 0,
                updated_at        REAL
            );

            CREATE TABLE IF NOT EXISTS trades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id  TEXT NOT NULL,
                side        TEXT NOT NULL,
                order_type  TEXT NOT NULL,
                order_id    TEXT,
                price       TEXT,
                size        TEXT,
                quote_total TEXT,
                fee         TEXT,
                reason      TEXT,
                created_at  REAL NOT NULL
            );
        """)
        self.conn.commit()

    def get_product_state(self, product_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM product_state WHERE product_id = ?", (product_id,)
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def upsert_product_state(self, product_id: str, **fields):
        fields["updated_at"] = time.time()
        existing = self.get_product_state(product_id)
        if existing is None:
            fields["product_id"] = product_id
            cols = ", ".join(fields.keys())
            placeholders = ", ".join("?" for _ in fields)
            self.conn.execute(
                f"INSERT INTO product_state ({cols}) VALUES ({placeholders})",
                list(fields.values()),
            )
        else:
            sets = ", ".join(f"{k} = ?" for k in fields)
            self.conn.execute(
                f"UPDATE product_state SET {sets} WHERE product_id = ?",
                list(fields.values()) + [product_id],
            )
        self.conn.commit()

    def set_rebuy_order(self, product_id: str, order_id: str, price: Decimal, size: Decimal):
        self.upsert_product_state(
            product_id,
            rebuy_order_id=order_id,
            rebuy_price=str(price),
            rebuy_size=str(size),
            rebuy_placed_at=time.time(),
        )

    def clear_rebuy_order(self, product_id: str):
        self.upsert_product_state(
            product_id,
            rebuy_order_id=None,
            rebuy_price=None,
            rebuy_size=None,
            rebuy_placed_at=0,
        )

    def increment_daily_trades(self, product_id: str):
        state = self.get_product_state(product_id)
        today = date.today().isoformat()
        if state and state["daily_trade_date"] == today:
            count = state["daily_trade_count"] + 1
        else:
            count = 1
        self.upsert_product_state(
            product_id, daily_trade_count=count, daily_trade_date=today
        )

    def get_daily_trade_count(self, product_id: str) -> int:
        state = self.get_product_state(product_id)
        if state is None:
            return 0
        today = date.today().isoformat()
        if state["daily_trade_date"] != today:
            return 0
        return state["daily_trade_count"]

    def record_trade(
        self, product_id: str, side: str, order_type: str, order_id: str,
        price: Decimal, size: Decimal, quote_total: Decimal, fee: Decimal, reason: str,
    ):
        self.conn.execute(
            """INSERT INTO trades
               (product_id, side, order_type, order_id, price, size, quote_total, fee, reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (product_id, side, order_type, order_id, str(price), str(size),
             str(quote_total), str(fee), reason, time.time()),
        )
        self.conn.commit()

    def get_recent_trades(self, product_id: str, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM trades WHERE product_id = ? ORDER BY created_at DESC LIMIT ?",
            (product_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        self.conn.close()
