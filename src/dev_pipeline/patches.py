from __future__ import annotations

import shutil
import subprocess
import tempfile
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
        with tempfile.TemporaryDirectory(prefix="pipeline-validator-") as temp_dir:
            worktree = Path(temp_dir) / "worktree"
            prepare = subprocess.run(
                [git, "worktree", "add", "--detach", str(worktree), "HEAD"],
                cwd=self.project_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if prepare.returncode != 0:
                detail = (prepare.stderr or prepare.stdout).strip()[-4000:]
                raise PipelineError(
                    f"Cannot prepare patch validation worktree: {detail}",
                    code="git_command_failed",
                )
            try:
                result = subprocess.run(
                    [git, "apply", "--3way", "--ignore-space-change", "-"],
                    cwd=worktree,
                    input=patch,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
            finally:
                subprocess.run(
                    [git, "worktree", "remove", "--force", str(worktree)],
                    cwd=self.project_root,
                    capture_output=True,
                    check=False,
                )
                subprocess.run(
                    [git, "worktree", "prune"],
                    cwd=self.project_root,
                    capture_output=True,
                    check=False,
                )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-4000:]
            raise PipelineError(
                f"Generated patch failed git apply --3way --ignore-space-change: {detail}",
                code="invalid_generated_patch",
            )
