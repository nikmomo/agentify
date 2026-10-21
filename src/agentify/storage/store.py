"""SQLite metadata + YAML workflow files (one file per skill version).

Learning evidence (executions, traces, verifications) and executable workflows are stored
separately: evidence never edits a workflow (docs/SKILL_LIFECYCLE.md §1).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentify.errors import NotFound
from agentify.workflows.loader import dump_workflow, load_workflow_text
from agentify.workflows.models import Workflow

SCHEMA = """
CREATE TABLE IF NOT EXISTS skills (
    skill_id TEXT PRIMARY KEY,
    app_key TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    active_version INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS skill_versions (
    skill_id TEXT NOT NULL REFERENCES skills(skill_id),
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    workflow_yaml TEXT NOT NULL,
    verified_param_sets TEXT NOT NULL DEFAULT '[]',
    verified_runs INTEGER NOT NULL DEFAULT 0,
    failed_runs INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY (skill_id, version)
);
CREATE TABLE IF NOT EXISTS executions (
    execution_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    app_id TEXT NOT NULL,
    app_key TEXT NOT NULL,
    goal TEXT NOT NULL DEFAULT '',
    skill_id TEXT,
    version INTEGER,
    params TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    outcome TEXT,
    verified INTEGER NOT NULL DEFAULT 0,
    action_count INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    failure TEXT,
    trace TEXT NOT NULL DEFAULT '[]',
    verifications TEXT NOT NULL DEFAULT '[]',
    started_at TEXT NOT NULL,
    ended_at TEXT
);
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class Store:
    def __init__(self, db_path: Path, skills_dir: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        skills_dir.mkdir(parents=True, exist_ok=True)
        self.skills_dir = skills_dir
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def _q(self, sql: str, args: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._db.execute(sql, args))

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ skills

    def skill_exists(self, skill_id: str) -> bool:
        return bool(self._q("SELECT 1 FROM skills WHERE skill_id=?", (skill_id,)))

    def insert_skill(self, wf: Workflow) -> None:
        ts = now_iso()
        with self.tx() as db:
            db.execute(
                "INSERT INTO skills VALUES (?,?,?,?,?,?,?)",
                (wf.id, wf.app_key, wf.name, wf.description, None, ts, ts),
            )
            self._insert_version(db, wf)

    def insert_version(self, wf: Workflow) -> None:
        with self.tx() as db:
            self._insert_version(db, wf)
            db.execute(
                "UPDATE skills SET name=?, description=?, updated_at=? WHERE skill_id=?",
                (wf.name, wf.description, now_iso(), wf.id),
            )

    def _insert_version(self, db: sqlite3.Connection, wf: Workflow) -> None:
        db.execute(
            "INSERT INTO skill_versions (skill_id, version, status, workflow_yaml, created_at)"
            " VALUES (?,?,?,?,?)",
            (wf.id, wf.version, wf.status, dump_workflow(wf), now_iso()),
        )
        self._write_yaml(wf)

    def _write_yaml(self, wf: Workflow) -> None:
        folder = self.skills_dir / wf.app_key / wf.id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"v{wf.version}.yaml").write_text(dump_workflow(wf), encoding="utf-8")

    def set_version_status(self, skill_id: str, version: int, status: str) -> None:
        wf = self.workflow(skill_id, version).model_copy(update={"status": status})
        with self.tx() as db:
            db.execute(
                "UPDATE skill_versions SET status=?, workflow_yaml=? WHERE skill_id=? AND version=?",
                (status, dump_workflow(wf), skill_id, version),
            )
            if status == "active":
                db.execute(
                    "UPDATE skills SET active_version=?, updated_at=? WHERE skill_id=?",
                    (version, now_iso(), skill_id),
                )
        self._write_yaml(wf)

    def record_run(
        self, skill_id: str, version: int, *, verified: bool, params: dict[str, str]
    ) -> list[str]:
        """Count a run; returns the distinct verified parameter sets (canonical JSON) afterwards."""
        with self.tx() as db:
            row = db.execute(
                "SELECT verified_param_sets FROM skill_versions WHERE skill_id=? AND version=?",
                (skill_id, version),
            ).fetchone()
            if row is None:
                raise NotFound(f"skill {skill_id} v{version} not found")
            sets: list[str] = json.loads(row["verified_param_sets"])
            if verified:
                key = json.dumps(params, sort_keys=True)
                if key not in sets:
                    sets.append(key)
                db.execute(
                    "UPDATE skill_versions SET verified_runs=verified_runs+1, verified_param_sets=?"
                    " WHERE skill_id=? AND version=?",
                    (json.dumps(sets), skill_id, version),
                )
            else:
                db.execute(
                    "UPDATE skill_versions SET failed_runs=failed_runs+1 WHERE skill_id=? AND version=?",
                    (skill_id, version),
                )
            return sets

    def copy_run_stats(self, skill_id: str, src: int, dst: int) -> None:
        with self.tx() as db:
            row = db.execute(
                "SELECT verified_param_sets, verified_runs, failed_runs FROM skill_versions"
                " WHERE skill_id=? AND version=?",
                (skill_id, src),
            ).fetchone()
            if row is None:
                raise NotFound(f"skill {skill_id} v{src} not found")
            db.execute(
                "UPDATE skill_versions SET verified_param_sets=?, verified_runs=?, failed_runs=?"
                " WHERE skill_id=? AND version=?",
                (
                    row["verified_param_sets"],
                    row["verified_runs"],
                    row["failed_runs"],
                    skill_id,
                    dst,
                ),
            )

    def workflow(self, skill_id: str, version: int) -> Workflow:
        rows = self._q(
            "SELECT workflow_yaml FROM skill_versions WHERE skill_id=? AND version=?",
            (skill_id, version),
        )
        if not rows:
            raise NotFound(f"skill {skill_id!r} v{version} not found")
        return load_workflow_text(rows[0]["workflow_yaml"])

    def skill_row(self, skill_id: str) -> sqlite3.Row:
        rows = self._q("SELECT * FROM skills WHERE skill_id=?", (skill_id,))
        if not rows:
            raise NotFound(f"skill {skill_id!r} not found")
        return rows[0]

    def version_rows(self, skill_id: str) -> list[sqlite3.Row]:
        return self._q(
            "SELECT * FROM skill_versions WHERE skill_id=? ORDER BY version", (skill_id,)
        )

    def skill_ids(self, app_key: str | None = None) -> list[str]:
        if app_key:
            rows = self._q(
                "SELECT skill_id FROM skills WHERE app_key=? ORDER BY skill_id", (app_key,)
            )
        else:
            rows = self._q("SELECT skill_id FROM skills ORDER BY skill_id")
        return [r["skill_id"] for r in rows]

    # -------------------------------------------------------------- executions

    def insert_execution(self, row: dict[str, Any]) -> None:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self.tx() as db:
            db.execute(
                f"INSERT INTO executions ({cols}) VALUES ({marks})",
                tuple(_enc(v) for v in row.values()),
            )

    def update_execution(self, execution_id: str, **fields: Any) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as db:
            db.execute(
                f"UPDATE executions SET {sets} WHERE execution_id=?",
                (*(_enc(v) for v in fields.values()), execution_id),
            )

    def executions_for(self, skill_id: str) -> list[dict[str, Any]]:
        """Light execution rows for one skill (no traces), oldest first."""
        rows = self._q(
            "SELECT execution_id, mode, version, verified, action_count, duration_ms, started_at"
            " FROM executions WHERE skill_id=? ORDER BY started_at",
            (skill_id,),
        )
        return [dict(r) | {"verified": bool(r["verified"])} for r in rows]

    def execution(self, execution_id: str) -> dict[str, Any]:
        rows = self._q("SELECT * FROM executions WHERE execution_id=?", (execution_id,))
        if not rows:
            raise NotFound(f"execution {execution_id!r} not found")
        out = dict(rows[0])
        for key in ("params", "failure", "trace", "verifications"):
            if out.get(key) is not None:
                out[key] = json.loads(out[key])
        out["verified"] = bool(out["verified"])
        return out


def _enc(v: Any) -> Any:
    if isinstance(v, dict | list):
        return json.dumps(v, default=str)
    if isinstance(v, bool):
        return int(v)
    return v
