from __future__ import annotations

from typing import Any

from .agents import BaseAgent
from .contracts import ARTIFACT_NAMES, RunState, utc_now, validate_artifact
from .development import DevelopmentWorkspace
from .errors import PipelineError, ValidationError
from .patches import PatchValidator
from .storage import RunStore


class ExternalWorkflow:
    """Stage protocol used by a host application that owns Agent orchestration."""

    def __init__(
        self,
        store: RunStore,
        requirement_agent: BaseAgent,
        workspace: DevelopmentWorkspace,
        validator: PatchValidator,
        project: dict[str, Any],
        project_files: list[str],
    ) -> None:
        self.store = store
        self.requirement_agent = requirement_agent
        self.workspace = workspace
        self.validator = validator
        self.project = project
        self.project_files = project_files

    def start(
        self,
        reference: str,
        task_id: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> RunState:
        state = self.store.load_or_create(task_id)
        if state.completed_stages or self.store.state_path(task_id).exists():
            raise PipelineError(f"Run '{task_id}' already exists", code="run_exists")
        artifact = self.requirement_agent.execute({"reference": reference})
        validate_artifact("requirement", artifact, task_id)
        state.metadata.update(metadata or {})
        state.metadata.update({"reference": reference, "mode": "host_agent"})
        self.store.save_artifact(
            state, "requirement", ARTIFACT_NAMES["requirement"], artifact
        )
        state.completed_stages = ["requirement"]
        state.status = "awaiting_analysis"
        state.current_stage = "analysis"
        self.store.save_state(state)
        self._event(state, "external_run_started")
        return state

    def context(self, state: RunState, stage: str) -> dict[str, Any]:
        expected = self._next_stage(state)
        if stage != expected:
            raise PipelineError(
                f"Run '{state.task_id}' expects stage '{expected}', not '{stage}'",
                code="invalid_stage_order",
            )
        context: dict[str, Any] = {
            "task_id": state.task_id,
            "stage": stage,
            "project": self.project,
        }
        for dependency in self._dependencies(stage):
            context[dependency] = self.store.load_artifact(state, dependency)
        if stage == "analysis":
            context["project_files"] = self.project_files
        if stage == "development":
            context["worktree_path"] = state.metadata.get("development_worktree_path")
            context["revision_feedback"] = state.metadata.get("revision_feedback")
        return context

    def submit(self, state: RunState, stage: str, artifact: dict[str, Any]) -> RunState:
        if stage not in {"analysis", "review"}:
            raise ValidationError("External submit only accepts analysis or review artifacts")
        expected = self._next_stage(state)
        if stage != expected:
            raise PipelineError(
                f"Run '{state.task_id}' expects stage '{expected}', not '{stage}'",
                code="invalid_stage_order",
            )
        normalized = self._envelope(artifact)
        validate_artifact(stage, normalized, state.task_id)
        self.store.save_artifact(state, stage, ARTIFACT_NAMES[stage], normalized)
        state.completed_stages.append(stage)
        if stage == "analysis":
            state.status = "awaiting_development"
            state.current_stage = "development"
        else:
            state.status = "awaiting_human_review"
            state.current_stage = None
        self.store.save_state(state)
        self._event(state, "external_stage_submitted", stage=stage)
        return state

    def prepare(self, state: RunState) -> RunState:
        if self._next_stage(state) != "development" or state.status != "awaiting_development":
            raise PipelineError(
                f"Run '{state.task_id}' is not ready for development",
                code="invalid_stage_order",
            )
        path = self.workspace.prepare()
        state.metadata["development_worktree_path"] = str(path)
        state.status = "development_in_progress"
        self.store.save_state(state)
        self._event(state, "external_development_prepared", path=str(path))
        return state

    def begin_revision(self, state: RunState, feedback: dict[str, Any]) -> RunState:
        if state.status != "needs_revision":
            raise PipelineError(
                f"Run '{state.task_id}' is not awaiting revision",
                code="invalid_stage_order",
            )
        for stage in ("development", "review"):
            self.store.remove_artifact(state, stage)
        state.completed_stages = [
            stage
            for stage in state.completed_stages
            if stage not in {"development", "review"}
        ]
        state.metadata["revision_feedback"] = feedback
        state.status = "awaiting_development"
        state.current_stage = "development"
        state.error = None
        self.store.save_state(state)
        self._event(state, "external_revision_started")
        return state

    def capture(self, state: RunState, result: dict[str, Any]) -> RunState:
        if state.status != "development_in_progress":
            raise PipelineError(
                f"Run '{state.task_id}' has no prepared development workspace",
                code="invalid_stage_order",
            )
        artifact = self._envelope(self.workspace.capture(result))
        validate_artifact("development", artifact, state.task_id)
        self.validator.validate(artifact)
        self.store.save_artifact(
            state, "development", ARTIFACT_NAMES["development"], artifact
        )
        state.completed_stages.append("development")
        state.metadata.pop("development_worktree_path", None)
        if artifact["change_status"] == "already_satisfied":
            state.status = "no_changes_needed"
            state.current_stage = None
        else:
            state.status = "awaiting_review"
            state.current_stage = "review"
        self.store.save_state(state)
        self.workspace.cleanup()
        self._event(
            state,
            "external_development_captured",
            change_status=artifact["change_status"],
        )
        return state

    @staticmethod
    def _envelope(artifact: dict[str, Any]) -> dict[str, Any]:
        return {
            **artifact,
            "created_at": artifact.get("created_at", utc_now()),
            "errors": artifact.get("errors", []),
        }

    @staticmethod
    def _dependencies(stage: str) -> tuple[str, ...]:
        return {
            "analysis": ("requirement",),
            "development": ("requirement", "analysis"),
            "review": ("requirement", "analysis", "development"),
        }[stage]

    @staticmethod
    def _next_stage(state: RunState) -> str:
        for stage in ("analysis", "development", "review"):
            if stage not in state.completed_stages:
                return stage
        raise PipelineError(
            f"Run '{state.task_id}' has no pending external stage",
            code="run_already_completed",
        )

    def _event(self, state: RunState, event: str, **details: Any) -> None:
        self.store.append_event(state.task_id, {"event": event, **details})
