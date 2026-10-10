"""DHP ↔ A2A bridge: durable execution under the A2A task lifecycle.

The A2A gap (from protocol_gaps.md): "A planner may mark a subtask
failed because its deadline expired while the remote agent later posts
a completed artifact… A2A exposes state. It does not provide consensus
or global transaction semantics." When the remote worker dies mid-task,
stock A2A leaves the task hanging in WORKING — the work is lost.

The bridge: the A2A Task ID is the stable identity; DHP handoffs are
the execution attempts underneath. Worker death becomes a transient
DHP-internal event (orphan → recover) invisible at the A2A layer.

Lifecycle mapping:
  DHP pending/running/orphaned → A2A WORKING  (orphaned is transient:
      a standby is coming; stock A2A would hang here forever)
  DHP completed               → A2A COMPLETED (+ Artifact from result)
  DHP failed (DLQ)            → A2A FAILED

Uses the real a2a-sdk types (Task/TaskStatus/Artifact). Transport is
in-process for the prototype; the A2A JSON-RPC/HTTP layer is orthogonal
to the durability question.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Optional

from a2a.types import (
    Artifact,
    Message,
    Part,
    Role,
    Task,
    TaskState,
    TaskStatus,
)

from . import envelope as E
from .runner3 import Runner3
from .supervisor3 import Supervisor3


def _text_part(text: str) -> Part:
    return Part(text=text)


def _now_ts():
    from google.protobuf.timestamp_pb2 import Timestamp
    ts = Timestamp()
    ts.GetCurrentTime()
    return ts


class DHPBridge:
    """Binds A2A tasks to DHP handoffs. One bridge per host/store."""

    def __init__(self, root: str, bridge_id: Optional[str] = None):
        self.runner = Runner3(root, runner_id=bridge_id or "bridge")
        self.supervisor = Supervisor3(
            root, supervisor_id=(bridge_id or "bridge") + "-sup")
        self.tasks: Dict[str, Task] = {}          # a2a_task_id -> Task
        self.task_handoff: Dict[str, str] = {}    # a2a_task_id -> dhp hid
        self.handoff_task: Dict[str, str] = {}    # dhp hid -> a2a_task_id
        self._rebuild_mapping()

    def _rebuild_mapping(self) -> None:
        """Recover the A2A<->DHP binding from durable envelope provenance.
        A fresh bridge instance (new process, after a crash) rebuilds
        everything from the store — no in-memory state is load-bearing."""
        for hid in self.runner.store.all_hids():
            try:
                env = self.runner._load(hid)
            except Exception:
                continue
            tid = env.get("provenance", {}).get("a2a_task_id")
            if tid:
                self.task_handoff[tid] = hid
                self.handoff_task[hid] = tid
                if tid not in self.tasks:
                    # rehydrate a Task shell; _refresh_task fills status
                    task = Task(id=tid,
                                context_id=env["provenance"].get("trace_id", ""),
                                status=TaskStatus(
                                    state=TaskState.TASK_STATE_SUBMITTED,
                                    timestamp=_now_ts()))
                    self.tasks[tid] = task

    # ---- A2A-facing ----
    def submit_task(self, message_text: str,
                    task_kind: str = "prime.census",
                    inputs: Optional[Dict[str, Any]] = None,
                    output_schema: Optional[Dict[str, Any]] = None,
                    context_id: Optional[str] = None) -> Task:
        """Client submits work. Returns the A2A Task (SUBMITTED)."""
        task_id = f"task-{uuid.uuid4().hex[:12]}"
        task = Task(
            id=task_id,
            context_id=context_id or f"ctx-{uuid.uuid4().hex[:8]}",
            status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED,
                              timestamp=_now_ts()),
        )
        # Wrap in a DHP handoff: the durable execution underneath.
        schema = output_schema or {
            "type": "object",
            "properties": {
                "lo": {"type": "integer"}, "hi": {"type": "integer"},
                "prime_count": {"type": "integer"},
                "prime_sum": {"type": "integer"},
                "chunks_done": {"type": "integer"}},
            "required": ["lo", "hi", "prime_count", "prime_sum",
                         "chunks_done"]}
        env = E.build(
            task_kind=task_kind,
            inputs={"inline": inputs or {}},
            output_schema=schema,
            budget={"max_steps": 2000, "max_spend_usd": 500.0,
                    "max_wallclock_s": 5400.0, "max_attempts": 5},
            created_by=f"a2a-bridge:{task_id}")
        # record the A2A binding in the envelope metadata
        hid = self.runner.dispatch(env)
        self.tasks[task_id] = task
        self.task_handoff[task_id] = hid
        self.handoff_task[hid] = task_id
        # store the binding durably in the envelope
        e2 = self.runner._load(hid)
        e2["provenance"]["a2a_task_id"] = task_id
        self.runner._save(e2)
        self._refresh_task(task_id)
        return task

    def get_task(self, task_id: str) -> Task:
        """Client polls. Always reflects current DHP state."""
        self._refresh_task(task_id)
        return self.tasks[task_id]

    def _refresh_task(self, task_id: str) -> None:
        task = self.tasks[task_id]
        hid = self.task_handoff[task_id]
        env = self.runner._load(hid)
        st = env["execution"]["status"]
        if st == "completed":
            result = env["delivery"]["result"]
            artifact = Artifact(
                artifact_id=f"artifact-{hid[:12]}",
                name="result",
                description="DHP handoff result",
                parts=[_text_part(json.dumps(result))],
            )
            task.status.state = TaskState.TASK_STATE_COMPLETED
            task.status.timestamp.CopyFrom(_now_ts())
            # avoid duplicate artifacts on repeated refresh
            if not any(a.artifact_id == artifact.artifact_id
                       for a in task.artifacts):
                task.artifacts.append(artifact)
        elif st == "failed":
            task.status.state = TaskState.TASK_STATE_FAILED
            task.status.timestamp.CopyFrom(_now_ts())
            task.status.message.CopyFrom(Message(
                message_id=f"msg-{uuid.uuid4().hex[:8]}",
                role=Role.ROLE_AGENT,
                parts=[_text_part(
                    env["execution"].get("error", "handoff failed"))]))
        else:
            # pending / running / orphaned / suspended -> WORKING.
            # Orphaned is the key: stock A2A would hang here; DHP is
            # already reassigning underneath.
            task.status.state = TaskState.TASK_STATE_WORKING
            task.status.timestamp.CopyFrom(_now_ts())

    # ---- worker-facing (what agents B, C, ... call) ----
    def claim_for_task(self, task_id: str, worker_id: str):
        hid = self.task_handoff[task_id]
        r = Runner3(self.runner.store.root, runner_id=worker_id,
                    store=self.runner.store)
        return r.claim(hid), hid

    def recover_for_task(self, task_id: str, worker_id: str):
        hid = self.task_handoff[task_id]
        r = Runner3(self.runner.store.root, runner_id=worker_id,
                    store=self.runner.store)
        return r.recover(hid), hid

    def supervisor_scan(self):
        return self.supervisor.scan()

    def task_state(self, task_id: str) -> str:
        return TaskState.Name(self.get_task(task_id).status.state)
