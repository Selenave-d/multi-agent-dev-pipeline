from __future__ import annotations

from pathlib import Path
from typing import Any

from .agents import BaseAgent
from .contracts import ARTIFACT_NAMES, STAGES, RunState, utc_now, validate_artifact
from .errors import PipelineError, StageExecutionError
from .storage import RunStore


class Orchestrator:
    def __init__(
        self,
        store: RunStore,
        agents: dict[str, BaseAgent],
        *,
        max_retries: int = 1,
    ) -> None:
        self.store = store
        self.agents = agents
        self.max_retries = max(0, max_retries)

    def run(self, reference: str, task_id: str, *, resume: bool = False) -> RunState:
        state = self.store.load_or_create(task_id)
        if state.completed_stages and not resume:
            raise PipelineError(
                f"Run '{task_id}' already exists; pass --resume to continue safely",
                code="run_exists",
            )
        state.status = "running"
        state.error = None
        self.store.save_state(state)

        context: dict[str, Any] = {"reference": reference}
        for stage in STAGES:
            if stage in state.completed_stages:
                artifact = self.store.load_artifact(state, stage)
                validate_artifact(stage, artifact, task_id)
                context[stage] = artifact
                continue
            self._execute_stage(stage, context, state)
            context[stage] = self.store.load_artifact(state, stage)

        state.status = "awaiting_human_review"
        state.current_stage = None
        self.store.save_state(state)
        return state

    def _execute_stage(self, stage: str, context: dict[str, Any], state: RunState) -> None:
        agent = self.agents[stage]
        state.current_stage = stage
        self.store.save_state(state)
        last_error: Exception | None = None
        for _ in range(self.max_retries + 1):
            state.attempts[stage] = state.attempts.get(stage, 0) + 1
            self.store.save_state(state)
            try:
                artifact = agent.execute(context)
                validate_artifact(stage, artifact, state.task_id)
                self.store.save_artifact(state, stage, ARTIFACT_NAMES[stage], artifact)
                state.completed_stages.append(stage)
                state.current_stage = None
                state.error = None
                self.store.save_state(state)
                return
            except Exception as exc:  # boundary: adapters may raise arbitrary SDK errors
                last_error = exc
        state.status = "failed"
        state.error = {
            "stage": stage,
            "type": type(last_error).__name__,
            "message": str(last_error),
            "occurred_at": utc_now(),
            "recoverable_with": "--resume",
        }
        self.store.save_state(state)
        raise StageExecutionError(stage, str(last_error)) from last_error


def resolve_runs_dir(config_path: Path, configured: str) -> Path:
    path = Path(configured)
    return path if path.is_absolute() else config_path.parent / path
