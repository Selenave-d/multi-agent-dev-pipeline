from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .errors import PipelineError
from .formatting import DevelopmentFormatter


class DevelopmentWorkspace:
    """Owns the isolated edit workspace and turns file edits into a Git patch."""

    def __init__(
        self,
        project_root: Path,
        worktree_path: Path,
        *,
        formatter: DevelopmentFormatter | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.worktree_path = worktree_path.resolve()
        self.formatter = formatter or DevelopmentFormatter(self.project_root)
        self.event_callback = event_callback

    def prepare(self) -> Path:
        if self._git_text(["status", "--porcelain"], self.project_root):
            raise PipelineError(
                f"Project repository has uncommitted changes: {self.project_root}",
                code="project_dirty",
                retryable=False,
            )
        if self.worktree_path.exists():
            raise PipelineError(
                f"Development worktree already exists: {self.worktree_path}",
                code="worktree_exists",
            )
        self.worktree_path.parent.mkdir(parents=True, exist_ok=True)
        self._git(
            ["worktree", "add", "--detach", str(self.worktree_path), "HEAD"],
            self.project_root,
        )
        self._event("development_worktree_created", path=str(self.worktree_path))
        return self.worktree_path

    def cleanup(self) -> None:
        if self.worktree_path.exists():
            self._git(
                ["worktree", "remove", "--force", str(self.worktree_path)],
                self.project_root,
                check=False,
            )
        self._git(["worktree", "prune"], self.project_root, check=False)
        self._event("development_worktree_removed", path=str(self.worktree_path))

    def capture(self, response: dict[str, Any]) -> dict[str, Any]:
        self._git(["add", "--intent-to-add", "--all"], self.worktree_path)
        raw_patch = self._git(["diff", "--binary", "HEAD", "--"], self.worktree_path).stdout
        raw_patch_path = self.worktree_path.parent / "development.raw.patch"
        self._write_patch(raw_patch_path, raw_patch)
        files = self._changed_files()
        has_changes = bool(raw_patch.strip())
        declared = response.get("change_status")
        if declared == "already_satisfied" and has_changes:
            raise PipelineError(
                "Development declared already_satisfied but modified the worktree",
                code="inconsistent_development_result",
            )
        if declared == "changes_required" and not has_changes:
            raise PipelineError(
                "Development declared changes_required but made no worktree changes",
                code="empty_generated_change",
            )
        self._event("development_format_started", files=files)
        try:
            format_result = self.formatter.format(self.worktree_path, files)
        except Exception as exc:
            self._event(
                "development_format_failed",
                error_type=type(exc).__name__,
                message=str(exc),
            )
            raise
        self._git(["add", "--intent-to-add", "--all"], self.worktree_path)
        formatted_files = self._changed_files()
        unexpected = sorted(set(formatted_files) - set(files))
        if unexpected:
            self._event("development_format_failed", unexpected_files=unexpected)
            raise PipelineError(
                f"Formatter modified files outside the development change set: {unexpected}",
                code="unexpected_format_changes",
                retryable=False,
            )
        self._event(
            "development_format_completed",
            tool=format_result.tool,
            files=format_result.files,
            stdout=format_result.stdout,
            stderr=format_result.stderr,
        )
        patch = self._git(["diff", "--binary", "HEAD", "--"], self.worktree_path).stdout
        patch_path = self.worktree_path.parent / "development.patch"
        self._write_patch(patch_path, patch)
        if declared == "changes_required" and not patch.strip():
            raise PipelineError(
                "Development changes became empty after formatting",
                code="empty_generated_change",
            )
        changes = []
        if patch.strip():
            changes.append({"file": ", ".join(formatted_files), "diff": patch})
        self._event(
            "development_diff_generated",
            files=formatted_files,
            raw_patch=str(raw_patch_path),
            patch=str(patch_path),
        )
        return {
            "task_id": response.get("task_id"),
            "change_status": declared,
            "changes": changes,
            "commit_message": response.get("commit_message"),
        }

    def _changed_files(self) -> list[str]:
        output = self._git(
            ["diff", "--name-only", "-z", "HEAD", "--"], self.worktree_path
        ).stdout
        return [name for name in output.split("\0") if name]

    @staticmethod
    def _write_patch(path: Path, patch: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(patch)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    @staticmethod
    def _git(
        args: list[str], cwd: Path, *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-4000:]
            raise PipelineError(f"Git command failed: {detail}", code="git_command_failed")
        return result

    def _git_text(self, args: list[str], cwd: Path) -> str:
        return self._git(args, cwd).stdout.strip()

    def _event(self, event: str, **details: Any) -> None:
        if self.event_callback:
            self.event_callback({"event": event, "stage": "development", **details})
