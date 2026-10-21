"""Skill lifecycle for the MVP: learn (v1), run (replay + auto-promotion), update (vN+1).

States used: candidate -> active -> superseded. A single run never activates a skill: promotion
needs >= 2 verified runs and, for skills with inputs, >= N distinct verified parameter sets.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

from agentify.adapters.base import AdapterError, AppHandle, PlatformAdapter, RiskLevel
from agentify.config import Settings
from agentify.errors import AgentifyError, InvalidState, LearnError, NotFound
from agentify.learning.extract import extract_workflow, slugify_skill_id
from agentify.runtime.executions import ExecutionManager
from agentify.storage import Store
from agentify.storage.store import now_iso
from agentify.workflows.executor import render_expectation, run_workflow
from agentify.workflows.loader import dump_workflow, parse_workflow
from agentify.workflows.models import Workflow
from agentify.workflows.template import redact_secrets, validate_params
from agentify.workflows.verify import check


class AppNotRunning(AgentifyError):
    code = "app_not_running"


class SkillService:
    def __init__(
        self,
        adapter: PlatformAdapter,
        store: Store,
        settings: Settings,
        executions: ExecutionManager,
    ) -> None:
        self.adapter = adapter
        self.store = store
        self.settings = settings
        self.executions = executions
        # Opens an app that is not running (set by the MCP server); None = never open apps.
        self.launcher: Callable[[str], Any] | None = None

    # ------------------------------------------------------------------ learn

    def learn(
        self,
        execution_id: str,
        *,
        name: str,
        description: str,
        inputs: dict[str, dict[str, Any]] | None = None,
        bindings: dict[str, list[int]] | None = None,
        skill_id: str | None = None,
        keep_steps: list[int] | None = None,
        risk: RiskLevel | None = None,
        outputs: dict[str, Any] | None = None,
        optional_steps: list[int] | None = None,
    ) -> dict[str, Any]:
        ex = self.executions.get(execution_id)
        sid = skill_id or slugify_skill_id(name)
        if self.store.skill_exists(sid):
            raise InvalidState(
                f"skill {sid!r} already exists: use update_skill to create a new version"
            )
        record = self._live_record(execution_id)
        wf, used = extract_workflow(
            execution=record,
            skill_id=sid,
            app_key=ex.app_key,
            name=name,
            description=description,
            inputs=inputs or {},
            bindings=bindings or {},
            keep_steps=keep_steps,
            risk=risk,
            outputs=outputs,
            optional_steps=optional_steps,
            notes=f"learned from exploration: {ex.goal}" if ex.goal else "learned from exploration",
        )
        self.store.insert_skill(wf)
        self.store.record_run(sid, 1, verified=True, params=used)
        self.executions.close(execution_id, "success", skill_id=sid, version=1, params=used)
        return self.summary(sid) | {
            "learned_from": execution_id,
            "learning_params": used,
            "warnings": verification_warnings(wf),
        }

    def _is_live(self, execution_id: str) -> bool:
        try:
            self.executions.get(execution_id)
            return True
        except NotFound:
            return False

    def _stored_record(self, execution_id: str) -> dict[str, Any]:
        """A recorded exploration from another (finished) process, e.g. the Explorer that just
        completed a task: its trace and verifications are persisted as they happen."""
        rec = self.store.execution(execution_id)
        if rec.get("mode") != "explore":
            raise LearnError(f"{execution_id} is not an exploration")
        if not any(v["result"]["passed"] for v in rec.get("verifications") or []):
            raise LearnError(f"{execution_id} has no passed verification")
        return {
            "execution_id": execution_id,
            "trace": rec.get("trace") or [],
            "verifications": rec.get("verifications") or [],
            "goal": rec.get("goal") or "",
            "app_key": rec.get("app_key"),
        }

    def _live_record(self, execution_id: str) -> dict[str, Any]:
        ex = self.executions.get(execution_id)
        return {"execution_id": execution_id, "trace": ex.trace, "verifications": ex.verifications}

    # -------------------------------------------------------------------- run

    def run(
        self,
        skill_id: str,
        params: dict[str, Any],
        *,
        version: int | None = None,
        app_id: str | None = None,
        cli_only: bool = False,
    ) -> dict[str, Any]:
        """Replay a skill. cli_only=True turns the adapter's in-process UIA fast path off for this
        run (a second opinion when a run failed: platform flake or skill defect?)."""
        with self.executions.ui_lock:
            fast = getattr(self.adapter, "fast_path", None)
            if cli_only and fast is not None:
                self.adapter.fast_path = None  # type: ignore[attr-defined]
            try:
                result = self._run(skill_id, params, version=version, app_id=app_id)
            finally:
                if cli_only and fast is not None:
                    self.adapter.fast_path = fast  # type: ignore[attr-defined]
            if cli_only:
                result["backend"] = "cli"
            return result

    def _run(
        self,
        skill_id: str,
        params: dict[str, Any],
        *,
        version: int | None,
        app_id: str | None,
    ) -> dict[str, Any]:
        v = version or self.tool_version(skill_id)
        wf = self.store.workflow(skill_id, v)
        scope = self.executions.scope_app_key
        if scope and wf.app_key != scope:
            raise InvalidState(f"this explorer is scoped to {scope!r}; skill app is {wf.app_key!r}")
        if wf.status in ("rejected",):
            raise InvalidState(f"{skill_id} v{v} is {wf.status}")
        app = self._find_app(wf.app_key, app_id)
        ensure_visible = getattr(self.adapter, "ensure_visible", None)
        if ensure_visible is not None:
            ensure_visible(app)  # a replay acts on a restored, rendered window ("open the app")
        execution_id = f"exec_{uuid.uuid4().hex[:12]}"
        self.store.insert_execution(
            {
                "execution_id": execution_id,
                "mode": "replay",
                "app_id": app.app_id,
                "app_key": wf.app_key,
                "skill_id": skill_id,
                "version": v,
                "params": redact_secrets(wf.inputs, params),
                "status": "open",
                "started_at": now_iso(),
            }
        )
        result = run_workflow(
            self.adapter,
            app,
            wf,
            params,
            max_actions=self.settings.max_actions_per_execution,
            max_seconds=self.settings.max_execution_seconds,
            resolve_secret=self.executions.secrets.resolve,
        )
        self.store.update_execution(
            execution_id,
            status="closed",
            outcome="success" if result.verified else "failure",
            verified=result.verified,
            action_count=result.action_count,
            duration_ms=result.duration_ms,
            failure=result.failure.model_dump() if result.failure else None,
            trace=[s.model_dump() for s in result.steps],
            verifications=[x.model_dump() for x in result.verification],
            ended_at=now_iso(),
        )
        promoted = False
        if result.failure is None or result.failure.category not in (
            "bad_parameters",
            "app_not_running",
        ):
            self.store.record_run(
                skill_id,
                v,
                verified=result.verified,
                params=redact_secrets(wf.inputs, result.params),
            )
            promoted = self._maybe_promote(skill_id, v)
        return {
            "execution_id": execution_id,
            "skill_id": skill_id,
            "version": v,
            "verified": result.verified,
            "duration_ms": result.duration_ms,
            "action_count": result.action_count,
            "llm_calls": 0,
            "fallback_used": result.fallback_used,
            "failure": result.failure.model_dump() if result.failure else None,
            "verification": [x.model_dump(exclude_none=True) for x in result.verification],
            "outputs": result.outputs,
            "promoted_to_active": promoted,
            "status": self.store.workflow(skill_id, v).status,
        }

    def goal_state_holds(self, skill_id: str, version: int, params: dict[str, Any]) -> bool:
        """True if the workflow's final checks already pass for these inputs, before any action.
        Read-only. A task that consumes its own start state (log out, log in, clear a list) is in
        this situation right after the exploration that performed it."""
        wf = self.store.workflow(skill_id, version)
        if not wf.verification:
            return False
        values = validate_params(wf.inputs, params)
        values = {name: values.get(name, "") for name in wf.inputs}
        with self.executions.ui_lock:
            app = self._find_app(wf.app_key, None)
            for exp in wf.verification:
                quick = render_expectation(exp, values).model_copy(update={"timeout_ms": 0})
                if not check(self.adapter, app, quick).passed:
                    return False
        return True

    def _find_app(self, app_key: str, app_id: str | None) -> AppHandle:
        try:
            if app_id:
                return self.adapter.attach(app_id)
            apps = [
                a for a in self.adapter.list_applications() if a.process_name.lower() == app_key
            ]
            if not apps and self.launcher is not None:
                # Not running: open it first (Start menu or the executable seen before).
                self.launcher(app_key)
                apps = [
                    a for a in self.adapter.list_applications() if a.process_name.lower() == app_key
                ]
        except AdapterError as exc:
            raise AppNotRunning(f"cannot attach: {exc.message}") from exc
        except AgentifyError as exc:
            raise AppNotRunning(
                f"{app_key!r} is not running and could not be opened: {exc.message}"
            ) from exc
        if not apps:
            raise AppNotRunning(f"no running window for app {app_key!r}; start the app first")
        apps.sort(key=lambda a: (not a.is_foreground, -((a.width or 0) * (a.height or 0))))
        return self.adapter.attach(apps[0].app_id)

    def _maybe_promote(self, skill_id: str, version: int) -> bool:
        row = next(r for r in self.store.version_rows(skill_id) if r["version"] == version)
        if row["status"] != "candidate":
            return False
        wf = self.store.workflow(skill_id, version)
        distinct = len(json.loads(row["verified_param_sets"]))
        need = self.settings.promote_after_verified_param_sets if wf.inputs else 1
        if row["verified_runs"] < 2 or distinct < need:
            return False
        previous = self.store.skill_row(skill_id)["active_version"]
        if previous is not None and previous != version:
            self.store.set_version_status(skill_id, previous, "superseded")
        self.store.set_version_status(skill_id, version, "active")
        return True

    # ----------------------------------------------------------------- update

    def update(
        self,
        skill_id: str,
        *,
        execution_id: str | None = None,
        name: str | None = None,
        description: str | None = None,
        inputs: dict[str, dict[str, Any]] | None = None,
        bindings: dict[str, list[int]] | None = None,
        keep_steps: list[int] | None = None,
        verification: list[dict[str, Any]] | None = None,
        outputs: dict[str, Any] | None = None,
        optional_steps: list[int] | None = None,
    ) -> dict[str, Any]:
        """Create version N+1.

        With `execution_id`: re-learn the steps from a new verified exploration (candidate until it
        is promoted; the current active version keeps serving meanwhile). Without it: a metadata
        update (name/description/input descriptions) of the newest version, which keeps that
        version's status and verified history.
        """
        rows = self.store.version_rows(skill_id)
        if not rows:
            raise NotFound(f"skill {skill_id!r} not found")
        latest = rows[-1]["version"]
        try:
            base_v = self.tool_version(skill_id)
        except InvalidState:
            base_v = latest  # every version was rejected: a repair builds on the newest one
        base = self.store.workflow(skill_id, base_v)
        new_v = latest + 1
        if execution_id is not None:
            live = self._is_live(execution_id)
            record = self._live_record(execution_id) if live else self._stored_record(execution_id)
            goal = self.executions.get(execution_id).goal if live else record["goal"]
            ex_app_key = self.executions.get(execution_id).app_key if live else record["app_key"]
            if ex_app_key != base.app_key:
                raise LearnError(
                    f"execution app {ex_app_key!r} differs from skill app {base.app_key!r}"
                )
            wf, used = extract_workflow(
                execution=record,
                skill_id=skill_id,
                app_key=base.app_key,
                name=name or base.name,
                description=description or base.description,
                inputs=inputs
                if inputs is not None
                else {k: v.model_dump(exclude_none=True) for k, v in base.inputs.items()},
                bindings=bindings or {},
                version=new_v,
                parent_version=base.version,
                keep_steps=keep_steps,
                outputs=outputs
                if outputs is not None
                else {k: v.model_dump(exclude_none=True) for k, v in base.outputs.items()},
                optional_steps=optional_steps,
                notes=f"re-learned from exploration: {goal}"
                if goal
                else "re-learned from exploration",
            )
            self.store.insert_version(wf)
            self.store.record_run(skill_id, new_v, verified=True, params=used)
            if live:
                self.executions.close(
                    execution_id, "success", skill_id=skill_id, version=new_v, params=used
                )
            else:
                self.store.update_execution(
                    execution_id, status="closed", outcome="success", skill_id=skill_id,
                    version=new_v, params=used,
                )  # fmt: skip
            return self.summary(skill_id) | {
                "new_version": new_v,
                "learned_from": execution_id,
                "warnings": verification_warnings(wf),
            }

        if inputs is not None:
            merged = {k: v.model_dump(exclude_none=True) for k, v in base.inputs.items()}
            for key, patch in inputs.items():
                if key not in merged:
                    raise LearnError(
                        f"metadata update cannot add input {key!r}; re-learn with execution_id"
                    )
                if "type" in patch and patch["type"] != merged[key].get("type", "string"):
                    raise LearnError(f"metadata update cannot change the type of {key!r}")
                merged[key] |= patch
        else:
            merged = {k: v.model_dump(exclude_none=True) for k, v in base.inputs.items()}
        data = base.model_dump(mode="json")
        data |= {
            "version": new_v,
            "name": name or base.name,
            "description": description or base.description,
            "inputs": merged,
            "status": "candidate",
        }
        changed = [
            k for k, v in (("name", name), ("description", description), ("inputs", inputs)) if v
        ]
        if verification is not None:
            # Same steps, new oracle: history carries over (the steps are unchanged) and the next
            # run exercises the new checks. Recorded in provenance.
            data["verification"] = verification
            changed.append("verification")
        data["provenance"] = {
            "source_execution_id": base.provenance.source_execution_id,
            "parent_version": base.version,
            "created_by": "host-agent",
            "notes": "metadata update: " + (", ".join(changed) or "no-op"),
        }
        wf = parse_workflow(data)
        self.store.insert_version(wf)
        # Same steps and verification: the verified history carries over to the new version.
        self.store.copy_run_stats(skill_id, base.version, new_v)
        base_status = next(r["status"] for r in rows if r["version"] == base.version)
        # A repaired rejected version must be re-verified: it starts as a candidate.
        new_status = "candidate" if base_status == "rejected" else base_status
        if base_status != "rejected":
            self.store.set_version_status(skill_id, base.version, "superseded")
        self.store.set_version_status(skill_id, new_v, new_status)
        return self.summary(skill_id) | {
            "new_version": new_v,
            "warnings": verification_warnings(wf),
        }

    # ------------------------------------------------------------------ reads

    def tool_version(self, skill_id: str) -> int:
        """Version the generated tool serves: the active one, else the newest candidate."""
        active = self.store.skill_row(skill_id)["active_version"]
        if active is not None:
            return int(active)
        cands = [
            r["version"] for r in self.store.version_rows(skill_id) if r["status"] == "candidate"
        ]
        if not cands:
            raise InvalidState(f"skill {skill_id!r} has no runnable version")
        return int(max(cands))

    def summary(self, skill_id: str) -> dict[str, Any]:
        row = self.store.skill_row(skill_id)
        tv = self.tool_version(skill_id)
        wf = self.store.workflow(skill_id, tv)
        return {
            "skill_id": skill_id,
            "app_key": row["app_key"],
            "name": wf.name,
            "description": wf.description,
            "status": wf.status,
            "active_version": row["active_version"],
            "serving_version": tv,
            "inputs": {k: v.model_dump(exclude_none=True) for k, v in wf.inputs.items()},
            "outputs": {k: o.model_dump(exclude_none=True) for k, o in wf.outputs.items()},
            "risk": wf.risk.value,
            "step_count": len(wf.steps),
            "versions": [
                {
                    "version": r["version"],
                    "status": r["status"],
                    "verified_runs": r["verified_runs"],
                    "failed_runs": r["failed_runs"],
                    "distinct_param_sets": len(json.loads(r["verified_param_sets"])),
                    "created_at": r["created_at"],
                }
                for r in self.store.version_rows(skill_id)
            ],
        }

    def stats(self, skill_id: str) -> dict[str, Any]:
        """Discovery vs replay efficiency, from execution records (measured, not estimated)."""
        import statistics

        rows = self.store.executions_for(skill_id)
        explore = [r for r in rows if r["mode"] == "explore"]
        replays = [r for r in rows if r["mode"] == "replay"]
        ok = [r for r in replays if r["verified"] and r["duration_ms"] is not None]
        discovery_ms = sum(int(r["duration_ms"] or 0) for r in explore) or None
        replay_median = int(statistics.median(r["duration_ms"] for r in ok)) if ok else None
        return {
            "discovery_ms": discovery_ms,
            "discovery_ui_actions": sum(int(r["action_count"] or 0) for r in explore) or None,
            "replays": len(replays),
            "replays_verified": len(ok),
            "replay_median_ms": replay_median,
            "replay_llm_calls": 0,
            "speedup_vs_discovery": round(discovery_ms / replay_median, 1)
            if discovery_ms and replay_median
            else None,
        }

    def list(self, app_key: str | None = None) -> list[dict[str, Any]]:
        out = []
        for sid in self.store.skill_ids(app_key):
            try:
                out.append(self.summary(sid))
            except InvalidState:
                continue
        return out

    def serving_workflow(self, skill_id: str) -> Workflow:
        return self.store.workflow(skill_id, self.tool_version(skill_id))

    def workflow_yaml(self, skill_id: str, version: int | None = None) -> str:
        return dump_workflow(self.store.workflow(skill_id, version or self.tool_version(skill_id)))


def verification_warnings(wf: Workflow) -> list[str]:
    """Flag value checks that do not depend on any input: they may encode state that changes
    between runs (counters, timestamps) and then fail on replay although the task succeeded."""
    out = []
    for i, exp in enumerate(wf.verification):
        if exp.type not in ("ui_value_equals", "file_text_equals"):
            continue
        if "{{" not in (exp.expected or ""):
            out.append(
                f"verification[{i}] expects the constant {exp.expected!r}, which does not depend on any "
                "input; if it can differ between runs (a counter, a date), replace it with "
                "update_skill(verification=[...])"
            )
    return out
