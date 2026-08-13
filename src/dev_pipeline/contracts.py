from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .errors import ValidationError

STAGES = ("requirement", "analysis", "development", "review")
ARTIFACT_NAMES = {
    "requirement": "01_requirement.json",
    "analysis": "02_analysis.json",
    "development": "03_code_changes.json",
    "review": "04_review.json",
}
DECISION_ARTIFACT = "05_decision.json"
VERIFICATION_ARTIFACT = "06_verification.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def require_fields(data: dict[str, Any], *fields: str) -> None:
    missing = [name for name in fields if data.get(name) in (None, "", [])]
    if missing:
        raise ValidationError(f"Missing required field(s): {', '.join(missing)}")


def validate_artifact(stage: str, data: dict[str, Any], task_id: str | None = None) -> None:
    if stage not in STAGES:
        raise ValidationError(f"Unknown stage: {stage}")
    require_fields(data, "task_id", "created_at")
    if "errors" not in data:
        raise ValidationError("Missing required field(s): errors")
    if not isinstance(data["errors"], list):
        raise ValidationError("'errors' must be an array")
    if task_id and data["task_id"] != task_id:
        raise ValidationError(
            f"Artifact task_id '{data['task_id']}' does not match run '{task_id}'"
        )
    stage_fields = {
        "requirement": ("title", "description", "priority", "module"),
        "analysis": ("analysis",),
        "development": ("change_status", "commit_message"),
        "review": ("review_result", "issues", "summary"),
    }
    require_fields(data, *stage_fields[stage])
    if stage == "analysis":
        analysis = data["analysis"]
        if not isinstance(analysis, dict):
            raise ValidationError("'analysis' must be an object")
        change_status = analysis.get("change_status")
        if change_status not in {"already_satisfied", "changes_required"}:
            raise ValidationError(f"Unknown analysis change_status: {change_status}")
    if stage == "development":
        changes = data.get("changes")
        if not isinstance(changes, list):
            raise ValidationError("Development 'changes' must be an array")
        change_status = data["change_status"]
        if change_status == "already_satisfied" and changes:
            raise ValidationError("already_satisfied development must have no changes")
        if change_status == "changes_required" and not changes:
            raise ValidationError("changes_required development must contain changes")
        if change_status not in {"already_satisfied", "changes_required"}:
            raise ValidationError(f"Unknown development change_status: {change_status}")


@dataclass
class RunState:
    task_id: str
    status: str = "running"
    current_stage: str | None = None
    completed_stages: list[str] = field(default_factory=list)
    attempts: dict[str, int] = field(default_factory=dict)
    artifacts: dict[str, dict[str, str]] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "current_stage": self.current_stage,
            "completed_stages": self.completed_stages,
            "attempts": self.attempts,
            "artifacts": self.artifacts,
            "error": self.error,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunState:
        require_fields(data, "task_id", "status")
        for field_name, expected_type in (
            ("completed_stages", list),
            ("attempts", dict),
            ("artifacts", dict),
        ):
            if field_name not in data or not isinstance(data[field_name], expected_type):
                raise ValidationError(f"Run state field '{field_name}' has an invalid type")
        values = {key: data[key] for key in cls.__dataclass_fields__ if key in data}
        return cls(**values)
