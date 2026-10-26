"""
db.py — SQLite storage for Edge Watch
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Replaces sensor_data.csv as the store for real data.

Tables:
  readings  → real sensor readings (synthetic readings are never stored)
  alerts    → threshold / ML / watchdog alerts, with acknowledgement
  commands  → every STOP / RUN published (audit trail)
  meta      → one-time markers (e.g. CSV already imported)

One connection shared by all threads (MQTT, simulator, watchdog, Flask),
guarded by a lock. WAL mode so reads don't block the writer.
"""

import csv
import math
import os
import sqlite3
import threading
import time

FIELDS = ["aRMSx", "aRMSy", "aRMSz", "vRMSx", "vRMSy", "vRMSz", "temperature", "aucausticRMS"]

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS readings (
    id      INTEGER PRIMARY KEY,
    ts      INTEGER NOT NULL,          -- ms since epoch (backend receive time)
    machine TEXT    NOT NULL,          -- sensor MAC for now (multi-machine later)
    seq     INTEGER,
    {", ".join(f"{f} REAL" for f in FIELDS)},
    extra   REAL
);
CREATE INDEX IF NOT EXISTS idx_readings_ts         ON readings(ts);
CREATE INDEX IF NOT EXISTS idx_readings_machine_ts ON readings(machine, ts);

CREATE TABLE IF NOT EXISTS alerts (
    id        INTEGER PRIMARY KEY,
    ts        INTEGER NOT NULL,
    machine   TEXT,
    type      TEXT NOT NULL,
    severity  TEXT NOT NULL,           -- normal | warning | critical
    message   TEXT NOT NULL,
    simulated INTEGER NOT NULL DEFAULT 0,
    ack_ts    INTEGER,
    ack_by    TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);

CREATE TABLE IF NOT EXISTS commands (
    id      INTEGER PRIMARY KEY,
    ts      INTEGER NOT NULL,
    topic   TEXT,
    payload TEXT NOT NULL,             -- "1" = STOP, "0" = RUN
    reason  TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

ALERT_DEDUP_MS = 60_000   # same type + severity within 60 s → stored once


def _now_ms() -> int:
    return int(time.time() * 1000)


def _num(val):
    try:
        f = float(val)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


class Store:
    def __init__(self, path: str):
        self.path  = path
        self._lock = threading.Lock()
        self._db   = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)

    def _q(self, sql, args=()):
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def _x(self, sql, args=()):
        with self._lock:
            return self._db.execute(sql, args)

    # ── Readings ──────────────────────────────────────────────
    def add_reading(self, r: dict):
        self._x(
            f"INSERT INTO readings (ts, machine, seq, {', '.join(FIELDS)}, extra) "
            f"VALUES (?, ?, ?, {', '.join('?' * len(FIELDS))}, ?)",
            (int(r["timestamp"]), str(r.get("mac") or "unknown"), _num(r.get("seq")),
             *[_num(r.get(f)) for f in FIELDS], _num(r.get("extra"))),
        )

    @staticmethod
    def _row_out(row: dict) -> dict:
        """DB row → same shape the API always returned (timestamp, mac, …)."""
        row = dict(row)
        row["timestamp"] = row.pop("ts")
        row["mac"]       = row.pop("machine")
        row.pop("id", None)
        row.pop("rn", None)
        return row

    def history_range(self, since_ms=None, limit=1000) -> list[dict]:
        """Readings since `since_ms` (all if None), evenly thinned to at most `limit` rows."""
        where, args = ("WHERE ts >= ?", [since_ms]) if since_ms else ("", [])
        total = self._q(f"SELECT COUNT(*) AS n FROM readings {where}", args)[0]["n"]
        if total == 0:
            return []
        step = max(1, math.ceil(total / max(limit, 1)))
        rows = self._q(
            f"SELECT * FROM (SELECT *, ROW_NUMBER() OVER (ORDER BY ts DESC) AS rn FROM readings {where}) "
            f"WHERE (rn - 1) % ? = 0 ORDER BY ts",     # rn 1 = newest → the latest reading is always kept
            args + [step],
        )
        return [self._row_out(r) for r in rows]

    def history_page(self, start_ms=None, end_ms=None, q="", page=1, limit=20):
        """Newest first, filtered, paginated. Returns (rows, total)."""
        cond, args = [], []
        if start_ms is not None:
            cond.append("ts >= ?"); args.append(start_ms)
        if end_ms is not None:
            cond.append("ts < ?");  args.append(end_ms)
        if q:
            like = f"%{q}%"
            cond.append("(" + " OR ".join(["machine LIKE ?"] + [f"CAST({f} AS TEXT) LIKE ?" for f in FIELDS]) + ")")
            args += [like] * (1 + len(FIELDS))
        where = ("WHERE " + " AND ".join(cond)) if cond else ""
        total = self._q(f"SELECT COUNT(*) AS n FROM readings {where}", args)[0]["n"]
        limit = max(1, int(limit))
        page  = max(1, int(page))
        rows  = self._q(f"SELECT * FROM readings {where} ORDER BY ts DESC LIMIT ? OFFSET ?",
                        args + [limit, (page - 1) * limit])
        return [self._row_out(r) for r in rows], total

    def latest_readings(self, n=30) -> list[dict]:
        rows = self._q("SELECT * FROM readings ORDER BY ts DESC LIMIT ?", (n,))
        return [self._row_out(r) for r in reversed(rows)]

    # ── Alerts ────────────────────────────────────────────────
    def add_alert(self, alert: dict, simulated=False):
        """Store an alert. Returns the stored alert, or None if it repeats a recent one."""
        ts = int(alert.get("timestamp") or _now_ms())
        dup = self._q(
            "SELECT 1 FROM alerts WHERE type = ? AND severity = ? AND simulated = ? AND ts > ? LIMIT 1",
            (alert.get("type"), alert.get("severity"), int(simulated), ts - ALERT_DEDUP_MS),
        )
        if dup:
            return None
        cur = self._x(
            "INSERT INTO alerts (ts, machine, type, severity, message, simulated) VALUES (?, ?, ?, ?, ?, ?)",
            (ts, alert.get("machine"), alert.get("type"), alert.get("severity"), alert.get("message", ""), int(simulated)),
        )
        return self.alert(cur.lastrowid)

    @staticmethod
    def _alert_out(row: dict) -> dict:
        row = dict(row)
        row["timestamp"] = row.pop("ts")
        row["simulated"] = bool(row["simulated"])
        row["acknowledged"] = row["ack_ts"] is not None
        return row

    def alert(self, alert_id: int):
        rows = self._q("SELECT * FROM alerts WHERE id = ?", (alert_id,))
        return self._alert_out(rows[0]) if rows else None

    def alerts(self, limit=20, unacked_only=False) -> list[dict]:
        where = "WHERE ack_ts IS NULL AND severity != 'normal'" if unacked_only else ""
        rows = self._q(f"SELECT * FROM alerts {where} ORDER BY ts DESC, id DESC LIMIT ?", (limit,))
        return [self._alert_out(r) for r in rows]

    def ack_alert(self, alert_id: int, by: str):
        cur = self._x("UPDATE alerts SET ack_ts = ?, ack_by = ? WHERE id = ? AND ack_ts IS NULL",
                      (_now_ms(), by, alert_id))
        return self.alert(alert_id) if cur.rowcount else None

    # ── Commands (STOP / RUN audit) ───────────────────────────
    def add_command(self, payload: str, reason: str, topic: str = None):
        self._x("INSERT INTO commands (ts, topic, payload, reason) VALUES (?, ?, ?, ?)",
                (_now_ms(), topic, payload, reason))

    def commands(self, limit=20) -> list[dict]:
        rows = self._q("SELECT ts AS timestamp, topic, payload, reason FROM commands ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["command"] = {"1": "STOP", "0": "RUN"}.get(r["payload"], r["payload"])
        return rows

    # ── Maintenance ───────────────────────────────────────────
    def prune(self, retention_days: float) -> int:
        """Delete readings older than retention_days (alerts/commands are kept)."""
        if retention_days <= 0:
            return 0
        cutoff = _now_ms() - int(retention_days * 86_400_000)
        return self._x("DELETE FROM readings WHERE ts < ?", (cutoff,)).rowcount

    def import_csv(self, path: str) -> tuple[int, int]:
        """
        One-time import of the old sensor_data.csv. Returns (imported, skipped).
        Skips damaged rows (bad timestamp / numbers) and simulator rows (SIM:* MAC).
        """
        key = f"csv_imported:{os.path.abspath(path)}"
        if not os.path.isfile(path) or self._q("SELECT 1 FROM meta WHERE key = ?", (key,)):
            return 0, 0

        rows, skipped = [], 0
        with open(path, newline="", errors="replace") as f:
            reader = csv.DictReader((line.replace("\0", "") for line in f))
            for r in reader:
                ts, mac = _num((r.get("timestamp") or "").strip()), (r.get("mac") or "").strip()
                values = [_num(r.get(fld)) for fld in FIELDS]
                if ts is None or ts < 1e12 or mac.startswith("SIM") or any(v is None for v in values):
                    skipped += 1
                    continue
                rows.append((int(ts), mac or "unknown", _num(r.get("seq")), *values, _num(r.get("extra"))))

        with self._lock:
            self._db.execute("BEGIN")
            self._db.executemany(
                f"INSERT INTO readings (ts, machine, seq, {', '.join(FIELDS)}, extra) "
                f"VALUES (?, ?, ?, {', '.join('?' * len(FIELDS))}, ?)", rows)
            self._db.execute("INSERT INTO meta (key, value) VALUES (?, ?)",
                             (key, f"{len(rows)} imported, {skipped} skipped"))
            self._db.execute("COMMIT")
        return len(rows), skipped
