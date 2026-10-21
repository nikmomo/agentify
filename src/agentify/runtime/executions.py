"""Exploration executions: bounded, recorded sessions in which the host agent acts on an app.

Every action and verification is appended to the trace before its result is returned. The trace
is the only input to learning (agentify.learning.extract).
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from agentify.adapters.base import Action, AdapterError, AppHandle, Element, PlatformAdapter
from agentify.config import Settings
from agentify.errors import ApprovalRequired, BadParameters, InvalidState, LimitExceeded, NotFound
from agentify.secrets import SecretResolver, has_secret
from agentify.storage import Store
from agentify.storage.store import now_iso
from agentify.workflows.models import Expectation
from agentify.workflows.verify import VerificationResult, check


def element_summary(el: Element | None) -> dict[str, Any] | None:
    if el is None:
        return None
    out: dict[str, Any] = {"role": el.role, "name": el.name}
    if el.automation_id:
        out["automation_id"] = el.automation_id
        out["stable_automation_id"] = el.has_stable_automation_id
    if el.value is not None:
        out["value"] = el.value
    return out


@dataclass
class Execution:
    execution_id: str
    app: AppHandle
    goal: str
    started: float = field(default_factory=time.monotonic)
    trace: list[dict[str, Any]] = field(default_factory=list)
    verifications: list[dict[str, Any]] = field(default_factory=list)
    open: bool = True
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def app_key(self) -> str:
        return self.app.process_name.lower()


class ExecutionManager:
    def __init__(self, adapter: PlatformAdapter, store: Store, settings: Settings) -> None:
        self.adapter = adapter
        self.store = store
        self.settings = settings
        self._live: dict[str, Execution] = {}
        # One UI operation at a time on this desktop: hosts may issue tool calls in parallel
        # (e.g. a batch of skill runs), and interleaved UI input on one window corrupts both.
        self.ui_lock = threading.RLock()
        # Explore mode: only this app may be explored (process name, lowercase).
        self.scope_app_key: str | None = None
        self.secrets = SecretResolver(settings.secret_names, settings.secrets_file)

    def start(self, app_id: str, goal: str) -> Execution:
        app = self.adapter.attach(app_id)
        if self.scope_app_key and app.process_name.lower() != self.scope_app_key:
            raise InvalidState(
                f"this explorer is scoped to {self.scope_app_key!r}; {app.process_name!r} is out of scope"
            )
        ensure_visible = getattr(self.adapter, "ensure_visible", None)
        if ensure_visible is not None:
            ensure_visible(app)  # explore a restored, rendered window (no "bring it up" steps)
        ex = Execution(execution_id=f"exec_{uuid.uuid4().hex[:12]}", app=app, goal=goal)
        self.store.insert_execution(
            {
                "execution_id": ex.execution_id,
                "mode": "explore",
                "app_id": app.app_id,
                "app_key": ex.app_key,
                "goal": goal,
                "status": "open",
                "started_at": now_iso(),
            }
        )
        self._live[ex.execution_id] = ex
        return ex

    def get(self, execution_id: str) -> Execution:
        ex = self._live.get(execution_id)
        if ex is None:
            raise NotFound(
                f"no open execution {execution_id!r} (it may be closed or from an earlier server run)"
            )
        return ex

    def _guard(self, ex: Execution) -> None:
        if not ex.open:
            raise InvalidState(f"execution {ex.execution_id} is closed")
        if len(ex.trace) >= self.settings.max_actions_per_execution:
            raise LimitExceeded(f"action cap reached ({self.settings.max_actions_per_execution})")
        if time.monotonic() - ex.started > self.settings.max_explore_seconds:
            raise LimitExceeded(
                f"exploration time cap reached ({self.settings.max_explore_seconds} s)"
            )

    def perform(
        self, execution_id: str, action: Action, *, approved: bool = False
    ) -> dict[str, Any]:
        ex = self.get(execution_id)
        with ex.lock, self.ui_lock:
            self._guard(ex)
            risk = action.effective_risk
            if risk.at_least(self.settings.approval_threshold) and not approved:
                raise ApprovalRequired(
                    f"action risk {risk.value} needs explicit user approval (pass approved=true)",
                    details={
                        "risk": risk.value,
                        "threshold": self.settings.approval_threshold.value,
                    },
                )
            # A value given in clear (e.g. a password from the task) is recorded as its reference.
            recorded = action
            if self.secrets.allowed and (action.value or action.keys):
                recorded = action.model_copy(
                    update={
                        "value": self.secrets.mask(action.value),
                        "keys": self.secrets.mask(action.keys),
                    }
                )
            entry: dict[str, Any] = {
                "index": len(ex.trace),
                "action": recorded.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
                | {"type": action.type},
                "ts": now_iso(),
            }
            try:
                sent = action
                if has_secret(action.value) or has_secret(action.keys):
                    sent = action.model_copy(
                        update={
                            "value": self.secrets.resolve(action.value),
                            "keys": self.secrets.resolve(action.keys),
                        }
                    )
                res = self.adapter.perform(ex.app, sent)
                durable = None
                if res.element is not None:
                    try:
                        durable = res.element.durable_selector().model_dump(
                            exclude_none=True, exclude_defaults=True
                        )
                    except ValueError:
                        durable = None
                entry |= {
                    "ok": True,
                    "resolved_by": res.resolved_by,
                    "element": element_summary(res.element),
                    "durable_selector": durable,
                    "value": res.value,
                    "duration_ms": res.duration_ms,
                }
            except AdapterError as exc:
                entry |= {
                    "ok": False,
                    "error": {"code": exc.code, "message": exc.message, "details": exc.details},
                }
            ex.trace.append(entry)
            self.store.update_execution(ex.execution_id, trace=ex.trace, action_count=len(ex.trace))
            return entry

    def verify(self, execution_id: str, expectation: Expectation) -> VerificationResult:
        if has_secret(expectation.expected) or has_secret(expectation.path):
            raise BadParameters("secrets cannot be verified or read back")
        ex = self.get(execution_id)
        with ex.lock, self.ui_lock:
            if not ex.open:
                raise InvalidState(f"execution {ex.execution_id} is closed")
            result = check(self.adapter, ex.app, expectation)
            ex.verifications.append(
                {
                    "after_step": len(ex.trace),
                    "expectation": expectation.model_dump(mode="json", exclude_none=True),
                    "result": result.model_dump(mode="json"),
                }
            )
            self.store.update_execution(ex.execution_id, verifications=ex.verifications)
            return result

    def close(self, execution_id: str, outcome: str, **extra: Any) -> dict[str, Any]:
        ex = self.get(execution_id)
        with ex.lock:
            ex.open = False
            verified = bool(ex.verifications) and ex.verifications[-1]["result"]["passed"]
            self.store.update_execution(
                ex.execution_id,
                status="closed",
                outcome=outcome,
                verified=verified,
                duration_ms=int((time.monotonic() - ex.started) * 1000),
                ended_at=now_iso(),
                **extra,
            )
        self._live.pop(execution_id, None)
        return self.store.execution(execution_id)
