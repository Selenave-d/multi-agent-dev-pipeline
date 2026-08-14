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
        max_retries: int = 3,
        development_validator: Any | None = None,
    ) -> None:
        self.store = store
        self.agents = agents
        self.max_retries = max(0, max_retries)
        self.development_validator = development_validator

    def run(
        self,
        reference: str,
        task_id: str,
        *,
        resume: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> RunState:
        state = self.store.load_or_create(task_id)
        if state.completed_stages and not resume:
            raise PipelineError(
                f"Run '{task_id}' already exists; pass --resume to continue safely",
                code="run_exists",
            )
        state.status = "running"
        state.error = None
        state.metadata.update(metadata or {})
        state.metadata["reference"] = reference
        self.store.save_state(state)
        self._event(state, "run_started", resume=resume)

        context: dict[str, Any] = {"reference": reference}
        for stage in STAGES:
            if stage in state.completed_stages:
                artifact = self.store.load_artifact(state, stage)
                validate_artifact(stage, artifact, task_id)
                context[stage] = artifact
            else:
                self._execute_stage(stage, context, state)
                context[stage] = self.store.load_artifact(state, stage)
            if stage == "development" and self._is_already_satisfied(context[stage]):
                state.status = "no_changes_needed"
                state.current_stage = None
                self.store.save_state(state)
                self._event(state, "run_completed", status=state.status)
                return state

        state.status = "awaiting_human_review"
        state.current_stage = None
        self.store.save_state(state)
        self._event(state, "run_completed", status=state.status)
        return state

    def revise(self, task_id: str, feedback: dict[str, Any]) -> RunState:
        state = self.store.load_or_create(task_id)
        if state.status != "needs_revision":
            raise PipelineError(
                f"Run '{task_id}' is '{state.status}', expected 'needs_revision'",
                code="invalid_state_transition",
            )
        context = {
            stage: self.store.load_artifact(state, stage)
            for stage in ("requirement", "analysis")
        }
        context["revision_feedback"] = feedback
        state.completed_stages = [
            stage for stage in state.completed_stages if stage not in {"development", "review"}
        ]
        self.store.remove_artifact(state, "review")
        state.status = "revising"
        state.error = None
        self.store.save_state(state)
        self._event(state, "revision_started")
        for stage in ("development", "review"):
            self._execute_stage(stage, context, state)
            context[stage] = self.store.load_artifact(state, stage)
            if stage == "development" and self._is_already_satisfied(context[stage]):
                state.status = "no_changes_needed"
                state.current_stage = None
                self.store.save_state(state)
                self._event(state, "revision_completed", status=state.status)
                return state
        state.status = "awaiting_human_review"
        self.store.save_state(state)
        self._event(state, "revision_completed", status=state.status)
        return state

    @staticmethod
    def _is_already_satisfied(development: dict[str, Any]) -> bool:
        return development.get("change_status") == "already_satisfied"

    def _execute_stage(self, stage: str, context: dict[str, Any], state: RunState) -> None:
        agent = self.agents[stage]
        state.current_stage = stage
        self.store.save_state(state)
        self._event(state, "stage_started", stage=stage)
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 2):
            state.attempts[stage] = state.attempts.get(stage, 0) + 1
            self.store.save_state(state)
            self._event(state, "stage_attempt_started", stage=stage, attempt=attempt)
            try:
                artifact = agent.execute(context)
                validate_artifact(stage, artifact, state.task_id)
                if stage == "development" and self.development_validator:
                    self._event(state, "development_validation_started", stage=stage)
                    self.development_validator.validate(artifact)
                    self._event(state, "development_validation_passed", stage=stage)
                self.store.save_artifact(state, stage, ARTIFACT_NAMES[stage], artifact)
                state.completed_stages.append(stage)
                state.current_stage = None
                state.error = None
                self.store.save_state(state)
                self._event(state, "stage_completed", stage=stage, attempt=attempt)
                return
            except Exception as exc:  # boundary: adapters may raise arbitrary SDK errors
                last_error = exc
                if stage == "development":
                    context["development_validation_feedback"] = {
                        "type": type(exc).__name__,
                        "message": str(exc),
                    }
                self._event(
                    state,
                    "stage_attempt_failed",
                    stage=stage,
                    attempt=attempt,
                    error_type=type(exc).__name__,
                    message=str(exc),
                    will_retry=attempt <= self.max_retries,
                )
        state.status = "failed"
        state.error = {
            "stage": stage,
            "type": type(last_error).__name__,
            "message": str(last_error),
            "occurred_at": utc_now(),
            "recoverable_with": "--resume",
        }
        self.store.save_state(state)
        self._event(state, "run_failed", stage=stage, message=str(last_error))
        raise StageExecutionError(stage, str(last_error)) from last_error

    def _event(self, state: RunState, event: str, **details: Any) -> None:
        self.store.append_event(state.task_id, {"event": event, **details})


def resolve_runs_dir(config_path: Path, configured: str) -> Path:
    path = Path(configured)
    return path if path.is_absolute() else config_path.parent / path
