"""Persistencia de sinais observados e decisoes do agente."""

import sqlite3
from pathlib import Path
from threading import Lock


class LogRepository:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = Lock()
        with self.lock:
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY,
                    service TEXT NOT NULL,
                    source_sequence INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    event TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    status INTEGER,
                    error_name TEXT,
                    UNIQUE(service, source_sequence, timestamp)
                );
                CREATE INDEX IF NOT EXISTS events_service_id ON events(service, id);
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    target TEXT NOT NULL,
                    predicted_type TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    action TEXT NOT NULL,
                    verified INTEGER NOT NULL,
                    evidence_request_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS incident_memory (
                    target TEXT NOT NULL,
                    error_name TEXT NOT NULL,
                    action TEXT NOT NULL,
                    verified_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(target,error_name,action)
                );
            """)
            columns = {row[1] for row in self.connection.execute("PRAGMA table_info(events)")}
            if "error_name" not in columns:
                self.connection.execute("ALTER TABLE events ADD COLUMN error_name TEXT")
            columns = {row[1] for row in self.connection.execute("PRAGMA table_info(decisions)")}
            if "error_name" not in columns:
                self.connection.execute("ALTER TABLE decisions ADD COLUMN error_name TEXT")
            if "strategy" not in columns:
                self.connection.execute("ALTER TABLE decisions ADD COLUMN strategy TEXT")
            self.connection.commit()

    def insert_events(self, service: str, events: list[dict]) -> int:
        rows = [(
            service,
            int(event["sequence"]),
            str(event["timestamp"])[:64],
            str(event["event"])[:80],
            str(event.get("request_id", ""))[:128],
            event.get("status") if isinstance(event.get("status"), int) else None,
            str(event["error_name"])[:80] if event.get("error_name") else None,
        ) for event in events]
        with self.lock:
            before = self.connection.total_changes
            self.connection.executemany(
                "INSERT OR IGNORE INTO events(service,source_sequence,timestamp,event,request_id,status,error_name) VALUES(?,?,?,?,?,?,?)",
                rows,
            )
            self.connection.commit()
            return self.connection.total_changes - before

    def recent_statuses(self, service: str, limit: int = 12, since: str = "") -> list[int]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT status FROM events WHERE service=? AND timestamp>=? AND status IS NOT NULL AND event IN ('service_response','backend_response') ORDER BY id DESC LIMIT ?",
                (service, since, limit),
            ).fetchall()
        return [int(row["status"]) for row in reversed(rows)]

    def recent_evidence(self, service: str, since: str = "") -> str:
        with self.lock:
            row = self.connection.execute(
                "SELECT request_id FROM events WHERE service=? AND timestamp>=? AND status>=500 ORDER BY id DESC LIMIT 1", (service, since)
            ).fetchone()
        return str(row["request_id"]) if row else ""

    def recent_error_names(self, service: str, since: str = "", limit: int = 12) -> list[str]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT error_name FROM events WHERE service=? AND timestamp>=? AND status>=500 AND error_name IS NOT NULL ORDER BY id DESC LIMIT ?",
                (service, since, limit),
            ).fetchall()
        return [str(row["error_name"]) for row in rows]

    def remembered_action(self, target: str, error_name: str, minimum: int = 3) -> str | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT action FROM incident_memory WHERE target=? AND error_name=? AND verified_count>=? ORDER BY verified_count DESC LIMIT 1",
                (target, error_name, minimum),
            ).fetchone()
        return str(row["action"]) if row else None

    def remember_success(self, target: str, error_name: str, action: str) -> None:
        if not error_name:
            return
        with self.lock:
            self.connection.execute(
                "INSERT INTO incident_memory(target,error_name,action,verified_count) VALUES(?,?,?,1) "
                "ON CONFLICT(target,error_name,action) DO UPDATE SET verified_count=verified_count+1",
                (target, error_name, action),
            )
            self.connection.commit()

    def memory_summary(self) -> list[dict]:
        with self.lock:
            rows = self.connection.execute("SELECT * FROM incident_memory ORDER BY target,error_name,verified_count DESC").fetchall()
        return [dict(row) for row in rows]

    def clear_memory(self) -> None:
        with self.lock:
            self.connection.execute("DELETE FROM incident_memory")
            self.connection.commit()

    def restore_memory(self, cases: list[dict]) -> None:
        with self.lock:
            with self.connection:
                self.connection.execute("DELETE FROM incident_memory")
                self.connection.executemany(
                    "INSERT INTO incident_memory(target,error_name,action,verified_count) VALUES(?,?,?,?)",
                    [(case["target"], case["error_name"], case["action"], case["verified_count"]) for case in cases],
                )

    def add_decision(self, timestamp: str, target: str, predicted_type: str, confidence: float,
                     action: str, verified: bool, evidence_request_id: str,
                     error_name: str = "", strategy: str = "rf") -> None:
        with self.lock:
            self.connection.execute(
                "INSERT INTO decisions(timestamp,target,predicted_type,confidence,action,verified,evidence_request_id,error_name,strategy) VALUES(?,?,?,?,?,?,?,?,?)",
                (timestamp, target, predicted_type, confidence, action, int(verified), evidence_request_id, error_name, strategy),
            )
            self.connection.commit()

    def recent_decisions(self, limit: int = 30) -> list[dict]:
        with self.lock:
            rows = self.connection.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def recent_events(self, limit: int = 100) -> list[dict]:
        with self.lock:
            rows = self.connection.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def counts(self) -> dict:
        with self.lock:
            rows = self.connection.execute("SELECT service,COUNT(*) AS count FROM events GROUP BY service").fetchall()
        return {str(row["service"]): int(row["count"]) for row in rows}
