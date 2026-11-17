"""SQLite 读写：批次快照、运行清单、隔离行、键值状态。

只放数据访问，不放判定；调用方拿到的是普通字典。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

from app import config

_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    schema_id      TEXT PRIMARY KEY,
    version_label  TEXT NOT NULL,
    source_path    TEXT NOT NULL,
    source_sha256  TEXT NOT NULL,
    encoding       TEXT NOT NULL,
    bom            INTEGER NOT NULL,
    field_count    INTEGER NOT NULL,
    blocked_count  INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    payload_json   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id         TEXT PRIMARY KEY,
    batch_key      TEXT NOT NULL UNIQUE,
    old_schema_id  TEXT NOT NULL,
    new_schema_id  TEXT NOT NULL,
    version_seq    INTEGER NOT NULL,
    input_sha256   TEXT NOT NULL,
    ruleset        TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to   TEXT,
    summary_json   TEXT NOT NULL,
    payload_json   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quarantine (
    schema_id     TEXT NOT NULL,
    line          INTEGER,
    table_name    TEXT,
    column_name   TEXT,
    reason        TEXT NOT NULL,
    blocking      INTEGER NOT NULL,
    detail        TEXT,
    raw           TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS position_verdicts (
    evidence_hash TEXT NOT NULL,
    consumer      TEXT NOT NULL,
    run_id        TEXT NOT NULL,
    verdict       TEXT NOT NULL,
    reason        TEXT NOT NULL,
    mode          TEXT,
    activation    TEXT NOT NULL DEFAULT 'none',
    detail        TEXT,
    payload_json  TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (evidence_hash, consumer)
);
CREATE TABLE IF NOT EXISTS position_audit (
    audit_id      TEXT PRIMARY KEY,
    evidence_hash TEXT NOT NULL,
    action        TEXT NOT NULL,
    status        TEXT NOT NULL,
    consumers     TEXT NOT NULL,
    detail        TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quarantine_schema ON quarantine(schema_id);
CREATE INDEX IF NOT EXISTS idx_position_audit_hash ON position_audit(evidence_hash);
"""


def db_path() -> Path:
    raw = os.environ.get("FIELDMAP_DB", "").strip()
    path = Path(raw) if raw else config.DEFAULT_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _LOCK, _connect() as conn:
        conn.executescript(SCHEMA)


def reset() -> None:
    """测试与重建用：清空全部业务表。"""
    with _LOCK, _connect() as conn:
        conn.executescript(SCHEMA)
        for table in ("snapshots", "runs", "quarantine", "kv",
                      "position_verdicts", "position_audit"):
            conn.execute(f"DELETE FROM {table}")


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value):
    if not value:
        return None
    return json.loads(value)


# ---------- 快照 ----------

def save_snapshot(snapshot: dict) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO snapshots (schema_id, version_label, source_path, source_sha256,
                   encoding, bom, field_count, blocked_count, created_at, payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(schema_id) DO UPDATE SET
                   version_label=excluded.version_label,
                   source_path=excluded.source_path,
                   source_sha256=excluded.source_sha256,
                   encoding=excluded.encoding,
                   bom=excluded.bom,
                   field_count=excluded.field_count,
                   blocked_count=excluded.blocked_count,
                   created_at=excluded.created_at,
                   payload_json=excluded.payload_json""",
            (
                snapshot["schema_id"], snapshot["version_label"], snapshot["source_path"],
                snapshot["source_sha256"], snapshot["encoding"], int(bool(snapshot["bom"])),
                int(snapshot["field_count"]), int(snapshot["blocked_count"]),
                snapshot["created_at"], _dumps(snapshot),
            ),
        )


def load_snapshot(schema_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM snapshots WHERE schema_id=?", (schema_id,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def list_snapshots(limit: int = 20) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT schema_id, version_label, source_path, source_sha256, encoding, bom,
                      field_count, blocked_count, created_at
               FROM snapshots ORDER BY created_at DESC, schema_id LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def save_quarantine(schema_id: str, rows: list[dict]) -> None:
    with _LOCK, _connect() as conn:
        conn.execute("DELETE FROM quarantine WHERE schema_id=?", (schema_id,))
        conn.executemany(
            """INSERT INTO quarantine (schema_id, line, table_name, column_name, reason,
                   blocking, detail, raw) VALUES (?,?,?,?,?,?,?,?)""",
            [
                (
                    schema_id, int(row.get("line") or 0), row.get("table") or "",
                    row.get("column") or "", row["reason"], int(bool(row.get("blocking"))),
                    row.get("detail") or "", row.get("raw") or "",
                )
                for row in rows
            ],
        )


def list_quarantine(schema_id: str) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT line, table_name, column_name, reason, blocking, detail, raw
               FROM quarantine WHERE schema_id=? ORDER BY line, reason""",
            (schema_id,),
        ).fetchall()
    return [
        {
            "line": row["line"],
            "table": row["table_name"],
            "column": row["column_name"],
            "reason": row["reason"],
            "blocking": bool(row["blocking"]),
            "detail": row["detail"],
            "raw": row["raw"],
        }
        for row in rows
    ]


# ---------- 运行清单 ----------

def save_run(run: dict) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO runs (run_id, batch_key, old_schema_id, new_schema_id, version_seq,
                   input_sha256, ruleset, created_at, effective_from, effective_to,
                   summary_json, payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run["run_id"], run["batch_key"], run["old_schema_id"], run["new_schema_id"],
                int(run["version_seq"]), run["input_sha256"], run["ruleset"],
                run["created_at"], run["effective_from"], run["effective_to"],
                _dumps(run["summary"]), _dumps(run),
            ),
        )


def find_run_by_key(batch_key: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM runs WHERE batch_key=?", (batch_key,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def run_by_id(run_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def latest_run() -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM runs ORDER BY version_seq DESC LIMIT 1"
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def list_runs(limit: int = 30) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT summary_json FROM runs ORDER BY version_seq DESC, run_id LIMIT ?""",
            (limit,),
        ).fetchall()
    return [_loads(row["summary_json"]) for row in rows]


def count_runs() -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()
    return int(row["n"])


def next_version_seq() -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT COALESCE(MAX(version_seq), 0) AS n FROM runs").fetchone()
    return int(row["n"]) + 1


def current_version_seq() -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT COALESCE(MAX(version_seq), 0) AS n FROM runs").fetchone()
    return int(row["n"])


def list_windows() -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT run_id, effective_from, effective_to FROM runs ORDER BY version_seq"
        ).fetchall()
    return [dict(row) for row in rows]


# ---------- 键值 ----------

def set_kv(key: str, value) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO kv (k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, _dumps(value)),
        )


def get_kv(key: str, default=None):
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    return _loads(row["v"]) if row else default


# ---------- 列序裁定 ----------

def upsert_position_verdict(row: dict) -> None:
    """按 (证据指纹, 消费方) 幂等写入：同一结构重跑只更新裁定，不新增裁定记录。

    激活态（是否已切兼容视图）由切换/回滚动作单独维护，重跑裁定不得覆盖它。
    """
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO position_verdicts (evidence_hash, consumer, run_id, verdict, reason,
                   mode, activation, detail, payload_json, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(evidence_hash, consumer) DO UPDATE SET
                   run_id=excluded.run_id,
                   verdict=excluded.verdict,
                   reason=excluded.reason,
                   mode=excluded.mode,
                   detail=excluded.detail,
                   payload_json=json_set(excluded.payload_json, '$.activation',
                                         position_verdicts.activation),
                   updated_at=excluded.updated_at""",
            (
                row["evidence_hash"], row["consumer"], row["run_id"], row["verdict"],
                row["reason"], row.get("mode"), row.get("activation", "none"),
                row.get("detail") or "", _dumps(row), row["updated_at"],
            ),
        )


def list_position_verdicts(evidence_hash: str) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT payload_json FROM position_verdicts
               WHERE evidence_hash=? ORDER BY consumer""",
            (evidence_hash,),
        ).fetchall()
    return [_loads(row["payload_json"]) for row in rows]


def get_position_verdict(evidence_hash: str, consumer: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM position_verdicts WHERE evidence_hash=? AND consumer=?",
            (evidence_hash, consumer),
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def set_position_activation(evidence_hash: str, consumers: list[str], activation: str) -> int:
    """批量改激活态（none/view），返回更新条数；payload 里的 activation 同步。"""
    changed = 0
    with _LOCK, _connect() as conn:
        for consumer in consumers:
            row = conn.execute(
                "SELECT payload_json FROM position_verdicts WHERE evidence_hash=? AND consumer=?",
                (evidence_hash, consumer),
            ).fetchone()
            if row is None:
                continue
            payload = _loads(row["payload_json"])
            payload["activation"] = activation
            conn.execute(
                """UPDATE position_verdicts SET activation=?, payload_json=?, updated_at=?
                   WHERE evidence_hash=? AND consumer=?""",
                (activation, _dumps(payload), payload["updated_at"],
                 evidence_hash, consumer),
            )
            changed += 1
    return changed


def save_position_audit(row: dict) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO position_audit (audit_id, evidence_hash, action, status, consumers,
                   detail, created_at) VALUES (?,?,?,?,?,?,?)""",
            (
                row["audit_id"], row["evidence_hash"], row["action"], row["status"],
                _dumps(row.get("consumers") or []), row.get("detail") or "",
                row["created_at"],
            ),
        )


def list_position_audit(evidence_hash: str | None = None, limit: int = 30) -> list[dict]:
    with _LOCK, _connect() as conn:
        if evidence_hash:
            rows = conn.execute(
                """SELECT audit_id, evidence_hash, action, status, consumers, detail, created_at
                   FROM position_audit WHERE evidence_hash=?
                   ORDER BY created_at DESC, audit_id LIMIT ?""",
                (evidence_hash, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT audit_id, evidence_hash, action, status, consumers, detail, created_at
                   FROM position_audit ORDER BY created_at DESC, audit_id LIMIT ?""",
                (limit,),
            ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["consumers"] = _loads(item["consumers"])
        out.append(item)
    return out
