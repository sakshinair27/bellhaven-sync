"""SQLite ledger of runs, proposals and decisions.

Re-run safety comes from two layers:
  1. The matcher reads the CRM fresh, so anything already applied no longer shows up
     as a difference (duplicates / CHOW'd accounts are excluded from matching).
  2. Every proposal has a fingerprint (kind + subject + intended writes). A fingerprint
     that was ever decided (approved, applied, rejected) is never re-queued, so a
     rejected proposal stays rejected until the underlying facts change.
"""
import datetime as dt
import hashlib
import json
import sqlite3

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT, finished_at TEXT, status TEXT, meta TEXT
);
CREATE TABLE IF NOT EXISTS proposals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fingerprint TEXT UNIQUE NOT NULL,
  kind TEXT, subject TEXT, title TEXT,
  ops TEXT, evidence TEXT,
  status TEXT NOT NULL DEFAULT 'pending',  -- pending|applied|rejected|failed|conflict|superseded
  first_run INTEGER, last_run INTEGER,
  created_at TEXT, decided_at TEXT, decided_by TEXT, comment TEXT, result TEXT
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, proposal_id INTEGER, action TEXT, detail TEXT
);
"""

DECIDED = ("applied", "rejected")


def now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def fingerprint(p):
    def strip(d):
        return {k: v for k, v in (d or {}).items() if k != "note"}

    core = [p["kind"], p["subject"]] + [
        [o["op"], o.get("account_id") or o.get("contact_id") or o.get("ref"), strip(o.get("set") or o.get("fields"))]
        for o in p["ops"]
    ]
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()[:16]


class Store:
    def __init__(self, path=None):
        path = path or config.DB_PATH
        if str(path) != ":memory:":
            config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def audit(self, pid, action, detail):
        self.db.execute("INSERT INTO audit (ts, proposal_id, action, detail) VALUES (?,?,?,?)",
                        (now(), pid, action, json.dumps(detail) if not isinstance(detail, str) else detail))

    # ---------------------------------------------------------------- runs
    def start_run(self):
        cur = self.db.execute("INSERT INTO runs (started_at, status) VALUES (?, 'running')", (now(),))
        return cur.lastrowid

    def finish_run(self, run_id, status, meta):
        self.db.execute("UPDATE runs SET finished_at=?, status=?, meta=? WHERE id=?",
                        (now(), status, json.dumps(meta), run_id))

    def last_good_run(self):
        r = self.db.execute("SELECT * FROM runs WHERE status='ok' ORDER BY id DESC LIMIT 1").fetchone()
        return (dict(r) | {"meta": json.loads(r["meta"])}) if r else None

    def runs(self, limit=10):
        return [dict(r) | {"meta": json.loads(r["meta"] or "{}")}
                for r in self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))]

    # ----------------------------------------------------------- proposals
    def sync_proposals(self, run_id, proposals):
        """Upsert this run's proposals. Returns counts by outcome."""
        counts = {"new": 0, "still_pending": 0, "already_decided": 0, "superseded": 0}
        seen = set()
        self.db.execute("BEGIN")
        try:
            for p in proposals:
                fp = fingerprint(p)
                seen.add(fp)
                row = self.db.execute("SELECT id, status FROM proposals WHERE fingerprint=?", (fp,)).fetchone()
                if row is None:
                    cur = self.db.execute(
                        "INSERT INTO proposals (fingerprint, kind, subject, title, ops, evidence, status,"
                        " first_run, last_run, created_at) VALUES (?,?,?,?,?,?,'pending',?,?,?)",
                        (fp, p["kind"], p["subject"], p["title"], json.dumps(p["ops"]),
                         json.dumps(p["evidence"]), run_id, run_id, now()))
                    self.audit(cur.lastrowid, "proposed", {"run": run_id})
                    counts["new"] += 1
                elif row["status"] in DECIDED:
                    counts["already_decided"] += 1
                else:
                    # pending / failed / conflict: refresh evidence (the CRM may have moved on)
                    self.db.execute(
                        "UPDATE proposals SET last_run=?, ops=?, evidence=?, title=?,"
                        " status=CASE WHEN status='superseded' THEN 'pending' ELSE status END WHERE id=?",
                        (run_id, json.dumps(p["ops"]), json.dumps(p["evidence"]), p["title"], row["id"]))
                    counts["still_pending"] += 1
            # Anything open that this run no longer produces is stale: the world changed.
            for row in self.db.execute(
                    "SELECT id, fingerprint FROM proposals WHERE status IN ('pending','conflict')").fetchall():
                if row["fingerprint"] not in seen:
                    self.db.execute("UPDATE proposals SET status='superseded' WHERE id=?", (row["id"],))
                    self.audit(row["id"], "superseded", {"run": run_id})
                    counts["superseded"] += 1
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return counts

    def get(self, pid):
        r = self.db.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
        return self._row(r) if r else None

    def list(self, status=None):
        q, args = "SELECT * FROM proposals", ()
        if status:
            q, args = q + " WHERE status=?", (status,)
        return [self._row(r) for r in self.db.execute(q + " ORDER BY id", args)]

    def counts(self):
        return {r[0]: r[1] for r in self.db.execute("SELECT status, count(*) FROM proposals GROUP BY status")}

    @staticmethod
    def _row(r):
        d = dict(r)
        for k in ("ops", "evidence", "result"):
            d[k] = json.loads(d[k]) if d[k] else None
        return d

    def decide(self, pid, status, who, comment, result=None):
        self.db.execute(
            "UPDATE proposals SET status=?, decided_at=?, decided_by=?, comment=?, result=? WHERE id=?",
            (status, now(), who, comment, json.dumps(result) if result is not None else None, pid))
        self.audit(pid, status, {"by": who, "comment": comment, "result": result})

    def claim_for_apply(self, pid):
        """Atomically move pending/failed/conflict -> applying so a double click can't write twice."""
        cur = self.db.execute(
            "UPDATE proposals SET status='applying' WHERE id=? AND status IN ('pending','failed','conflict')",
            (pid,))
        return cur.rowcount == 1

    def audit_log(self, pid):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM audit WHERE proposal_id=? ORDER BY id", (pid,))]
