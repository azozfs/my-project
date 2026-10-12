"""SQLite storage for conversations and tickets."""

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEPARTMENTS = [
    "التأجير",
    "إدارة الأملاك",
    "الخدمات والصيانة",
    "المحاسبة",
    "اتحاد الملاك",
    "المبيعات",
]

TICKET_TYPES = ["شكوى", "صيانة", "مقترح", "طلب عقد أو تجديد", "استفسار", "عميل مهتم", "عرض عقار"]
TICKET_STATUSES = ["جديدة", "قيد المعالجة", "مغلقة"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT NOT NULL,
    started_at TEXT NOT NULL,
    last_activity TEXT NOT NULL,
    messages TEXT NOT NULL DEFAULT '[]',
    handed_off INTEGER NOT NULL DEFAULT 0,
    handoff_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_conv_phone ON conversations(phone, id);

CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    phone TEXT NOT NULL,
    customer_name TEXT,
    ticket_type TEXT NOT NULL,
    department TEXT NOT NULL,
    urgency TEXT NOT NULL,
    unit_ref TEXT,
    summary TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'جديدة',
    staff_note TEXT
);

CREATE TABLE IF NOT EXISTS processed_messages (
    message_id TEXT PRIMARY KEY,
    received_at TEXT NOT NULL
);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)

    # --- webhook de-duplication (Meta retries deliveries) ---

    def mark_processed(self, message_id: str) -> bool:
        """Return True the first time a message id is seen, False on repeats."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO processed_messages (message_id, received_at) VALUES (?, ?)",
                (message_id, _iso(_now())),
            )
            self._conn.commit()
            return cur.rowcount == 1

    # --- conversations ---

    def current_conversation(self, phone: str, gap_hours: int) -> dict:
        """The customer's open conversation, or a new one after a long silence."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM conversations WHERE phone = ? ORDER BY id DESC LIMIT 1", (phone,)
            ).fetchone()
            if row is not None:
                last = datetime.fromisoformat(row["last_activity"])
                if _now() - last < timedelta(hours=gap_hours):
                    return self._conv_dict(row)
            now = _iso(_now())
            cur = self._conn.execute(
                "INSERT INTO conversations (phone, started_at, last_activity) VALUES (?, ?, ?)",
                (phone, now, now),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM conversations WHERE id = ?", (cur.lastrowid,)).fetchone()
            return self._conv_dict(row)

    def save_messages(self, conv_id: int, messages: list) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE conversations SET messages = ?, last_activity = ? WHERE id = ?",
                (json.dumps(messages, ensure_ascii=False), _iso(_now()), conv_id),
            )
            self._conn.commit()

    def set_handoff(self, conv_id: int, handed_off: bool, reason: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE conversations SET handed_off = ?, handoff_reason = ? WHERE id = ?",
                (1 if handed_off else 0, reason, conv_id),
            )
            self._conn.commit()

    def resume_bot(self, phone: str) -> bool:
        """Staff hand the customer back to the bot. Returns False if no conversation exists."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE conversations SET handed_off = 0, handoff_reason = NULL "
                "WHERE id = (SELECT id FROM conversations WHERE phone = ? ORDER BY id DESC LIMIT 1)",
                (phone,),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def handed_off_conversations(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM conversations WHERE handed_off = 1 ORDER BY last_activity DESC"
            ).fetchall()
            return [self._conv_dict(r) for r in rows]

    @staticmethod
    def _conv_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["messages"] = json.loads(d["messages"])
        d["handed_off"] = bool(d["handed_off"])
        return d

    # --- tickets ---

    def create_ticket(
        self,
        phone: str,
        ticket_type: str,
        department: str,
        urgency: str,
        summary: str,
        customer_name: str | None,
        unit_ref: str | None,
    ) -> int:
        now = _iso(_now())
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO tickets (created_at, updated_at, phone, customer_name, ticket_type, "
                "department, urgency, unit_ref, summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (now, now, phone, customer_name, ticket_type, department, urgency, unit_ref, summary),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_ticket(self, ticket_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
            return dict(row) if row else None

    def tickets_for_phone(self, phone: str, limit: int = 5) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tickets WHERE phone = ? ORDER BY id DESC LIMIT ?", (phone, limit)
            ).fetchall()
            return [dict(r) for r in rows]

    def list_tickets(self, status: str | None = None, department: str | None = None) -> list[dict]:
        query, args = "SELECT * FROM tickets WHERE 1=1", []
        if status:
            query += " AND status = ?"
            args.append(status)
        if department:
            query += " AND department = ?"
            args.append(department)
        query += " ORDER BY id DESC LIMIT 500"
        with self._lock:
            return [dict(r) for r in self._conn.execute(query, args).fetchall()]

    def update_ticket(self, ticket_id: int, status: str, staff_note: str | None) -> bool:
        if status not in TICKET_STATUSES:
            raise ValueError(f"unknown status: {status}")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tickets SET status = ?, staff_note = ?, updated_at = ? WHERE id = ?",
                (status, staff_note, _iso(_now()), ticket_id),
            )
            self._conn.commit()
            return cur.rowcount == 1
