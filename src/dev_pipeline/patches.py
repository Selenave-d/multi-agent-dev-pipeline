from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from .errors import PipelineError, ValidationError


class PatchValidator:
    """Checks generated diffs against the target repository without modifying it."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()

    def validate(self, artifact: dict[str, Any]) -> None:
        if artifact["change_status"] == "already_satisfied":
            return
        if not (self.project_root / ".git").exists():
            raise ValidationError(f"Project root is not a Git repository: {self.project_root}")
        git = shutil.which("git")
        if not git:
            raise PipelineError("Command not found: git", code="provider_not_installed")
        status = subprocess.run(
            [git, "status", "--porcelain"],
            cwd=self.project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if status.returncode != 0:
            detail = (status.stderr or status.stdout).strip()[-4000:]
            raise PipelineError(f"git status failed: {detail}", code="git_command_failed")
        if status.stdout.strip():
            raise PipelineError(
                f"Project repository has uncommitted changes: {self.project_root}",
                code="project_dirty",
            )
        patch = "\n".join(str(change["diff"]).rstrip() for change in artifact["changes"]) + "\n"
        result = subprocess.run(
            [git, "apply", "--check", "-"],
            cwd=self.project_root,
            input=patch,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-4000:]
            raise PipelineError(
                f"Generated patch failed git apply --check: {detail}",
                code="invalid_generated_patch",
            )
