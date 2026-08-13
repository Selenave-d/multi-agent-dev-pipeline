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
        "development": ("changes", "commit_message"),
        "review": ("review_result", "issues", "summary"),
    }
    require_fields(data, *stage_fields[stage])


@dataclass
class RunState:
    task_id: str
    status: str = "running"
    current_stage: str | None = None
    completed_stages: list[str] = field(default_factory=list)
    attempts: dict[str, int] = field(default_factory=dict)
    artifacts: dict[str, dict[str, str]] = field(default_factory=dict)
    error: dict[str, Any] | None = None
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
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunState:
        require_fields(data, "task_id", "status", "completed_stages", "attempts", "artifacts")
        return cls(**{key: data.get(key) for key in cls.__dataclass_fields__})
