from __future__ import annotations

import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from .contracts import DECISION_ARTIFACT, VERIFICATION_ARTIFACT, RunState, utc_now
from .errors import PipelineError, ValidationError
from .storage import RunStore


class WorktreeExecutor:
    """Applies approved patches and verifies them in an isolated Git worktree."""

    def __init__(
        self,
        store: RunStore,
        project_root: Path,
        worktree_root: Path,
        commands: dict[str, str],
        *,
        command_timeout: int = 1200,
    ) -> None:
        self.store = store
        self.project_root = project_root.resolve()
        self.worktree_root = worktree_root.resolve()
        self.commands = commands
        self.command_timeout = command_timeout

    def approve(self, state: RunState) -> RunState:
        self._require_status(state, "awaiting_human_review")
        self._event(state, "approval_started")
        decision = {
            "task_id": state.task_id,
            "decision": "approve",
            "created_at": utc_now(),
            "errors": [],
        }
        self.store.save_artifact(state, "decision", DECISION_ARTIFACT, decision)
        state.status = "approved"
        state.error = None
        self.store.save_state(state)
        try:
            self._prepare_worktree(state)
            self._apply_patch(state)
            return self._verify(state)
        except Exception as exc:
            state.status = "needs_revision"
            state.error = {
                "stage": "apply_or_verify",
                "type": type(exc).__name__,
                "message": str(exc),
                "occurred_at": utc_now(),
                "recoverable_with": f"dev-pipeline revise --task-id {state.task_id}",
            }
            self.store.save_state(state)
            self._event(state, "approval_failed", message=str(exc))
            return state

    def reject(self, state: RunState) -> RunState:
        self._require_status(state, "awaiting_human_review")
        decision = {
            "task_id": state.task_id,
            "decision": "reject",
            "created_at": utc_now(),
            "errors": [],
        }
        self.store.save_artifact(state, "decision", DECISION_ARTIFACT, decision)
        state.status = "rejected"
        state.error = None
        self.store.save_state(state)
        self._event(state, "task_rejected")
        return state

    def cleanup_for_revision(self, state: RunState) -> None:
        self._require_status(state, "needs_revision")
        worktree = self._worktree_path(state.task_id)
        branch = state.metadata.get("worktree_branch")
        if worktree.exists():
            self._git(["worktree", "remove", "--force", str(worktree)], cwd=self.project_root)
        self._git(["worktree", "prune"], cwd=self.project_root)
        if branch:
            result = self._git(
                ["branch", "--delete", "--force", str(branch)],
                cwd=self.project_root,
                check=False,
            )
            if result.returncode not in {0, 1}:
                self._raise_command_error(result, "git branch --delete")
        state.metadata.pop("worktree_path", None)
        state.metadata.pop("worktree_branch", None)
        self.store.save_state(state)

    def merge(self, state: RunState) -> RunState:
        self._require_status(state, "ready_to_merge")
        worktree = Path(str(state.metadata.get("worktree_path", ""))).resolve()
        branch = str(state.metadata.get("worktree_branch", ""))
        base_commit = str(state.metadata.get("base_commit", ""))
        base_branch = str(state.metadata.get("base_branch", ""))
        if not worktree.is_dir() or not branch or not base_commit or not base_branch:
            raise PipelineError("Worktree metadata is incomplete", code="invalid_worktree_state")
        if self._git_text(["status", "--porcelain"], cwd=self.project_root):
            raise PipelineError("Main worktree is not clean", code="main_worktree_dirty")
        current_branch = self._git_text(["branch", "--show-current"], cwd=self.project_root)
        if current_branch != base_branch:
            raise PipelineError(
                f"Main worktree is on '{current_branch}', expected '{base_branch}'",
                code="base_branch_changed",
            )
        current_head = self._git_text(["rev-parse", "HEAD"], cwd=self.project_root)
        if current_head != base_commit:
            raise PipelineError(
                "Main branch advanced after approval; rerun verification on the new base",
                code="base_commit_changed",
            )
        development = self.store.load_artifact(state, "development")
        self._git(["add", "--all"], cwd=worktree)
        if not self._git_text(["status", "--porcelain"], cwd=worktree):
            raise PipelineError("Approved patch produced no changes", code="empty_patch")
        self._git(["commit", "-m", str(development["commit_message"])], cwd=worktree)
        merge_message = f"merge: {state.task_id}"
        result = self._git(
            ["merge", "--no-ff", branch, "-m", merge_message],
            cwd=self.project_root,
            check=False,
        )
        if result.returncode != 0:
            self._git(["merge", "--abort"], cwd=self.project_root, check=False)
            raise PipelineError(
                f"Git merge failed: {self._command_detail(result)}",
                code="merge_conflict",
            )
        state.status = "merged"
        state.error = None
        state.metadata["merge_commit"] = self._git_text(
            ["rev-parse", "HEAD"], cwd=self.project_root
        )
        self.store.save_state(state)
        return state

    def _prepare_worktree(self, state: RunState) -> None:
        if not (self.project_root / ".git").exists():
            raise ValidationError(f"Project root is not a Git repository: {self.project_root}")
        if self._git_text(["status", "--porcelain"], cwd=self.project_root):
            raise PipelineError(
                "Project worktree must be clean before approval",
                code="project_dirty",
            )
        base_branch = self._git_text(["branch", "--show-current"], cwd=self.project_root)
        if not base_branch:
            raise PipelineError("Detached HEAD is not supported", code="detached_head")
        base_commit = self._git_text(["rev-parse", "HEAD"], cwd=self.project_root)
        worktree = self._worktree_path(state.task_id)
        if worktree.exists():
            raise PipelineError(f"Worktree already exists: {worktree}", code="worktree_exists")
        branch = f"pipeline/{state.task_id.lower()}"
        state.metadata.update(
            {
                "project_root": str(self.project_root),
                "base_branch": base_branch,
                "base_commit": base_commit,
                "worktree_branch": branch,
                "worktree_path": str(worktree),
            }
        )
        self.store.save_state(state)
        self._git(
            ["worktree", "add", "-b", branch, str(worktree), base_commit],
            cwd=self.project_root,
        )
        self._event(state, "approval_worktree_created", path=str(worktree))

    def _apply_patch(self, state: RunState) -> None:
        worktree = Path(state.metadata["worktree_path"])
        development = self.store.load_artifact(state, "development")
        diffs = [str(change["diff"]).rstrip() for change in development["changes"]]
        patch_text = "\n".join(diffs) + "\n"
        patch_path = self.store.run_dir(state.task_id) / "changes.patch"
        patch_path.write_text(patch_text, encoding="utf-8", newline="\n")
        self._git(["apply", "--check", str(patch_path)], cwd=worktree)
        self._git(["apply", str(patch_path)], cwd=worktree)
        self._event(state, "patch_applied", patch=str(patch_path))

    def _verify(self, state: RunState) -> RunState:
        worktree = Path(state.metadata["worktree_path"])
        results: list[dict[str, Any]] = []
        for name in ("lint", "test", "build"):
            command = self.commands.get(name)
            if not command:
                results.append({"name": name, "command": None, "status": "skipped"})
                continue
            started = time.monotonic()
            self._event(state, "verification_command_started", name=name, command=command)
            try:
                result = subprocess.run(
                    command,
                    cwd=worktree,
                    shell=True,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.command_timeout,
                    check=False,
                )
                record = {
                    "name": name,
                    "command": command,
                    "status": "passed" if result.returncode == 0 else "failed",
                    "exit_code": result.returncode,
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "stdout": self.store.redact(result.stdout[-10_000:]),
                    "stderr": self.store.redact(result.stderr[-10_000:]),
                }
            except subprocess.TimeoutExpired as exc:
                record = {
                    "name": name,
                    "command": command,
                    "status": "failed",
                    "exit_code": None,
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "stdout": self.store.redact(self._decode_timeout(exc.stdout)),
                    "stderr": self.store.redact(self._decode_timeout(exc.stderr)),
                    "message": f"Timed out after {self.command_timeout}s",
                }
            results.append(record)
            output = self.store.save_tool_output(
                state.task_id,
                "verification",
                name,
                stdout=str(record.get("stdout", "")),
                stderr=str(record.get("stderr", "")),
            )
            self._event(
                state,
                "verification_command_finished",
                name=name,
                status=record["status"],
                exit_code=record.get("exit_code"),
                duration_seconds=record.get("duration_seconds"),
                output=output,
            )
            if record["status"] == "failed":
                break
        passed = all(item["status"] in {"passed", "skipped"} for item in results)
        verification = {
            "task_id": state.task_id,
            "result": "passed" if passed else "failed",
            "steps": results,
            "created_at": utc_now(),
            "errors": [],
        }
        self.store.save_artifact(
            state,
            "verification",
            VERIFICATION_ARTIFACT,
            verification,
        )
        if passed:
            state.status = "ready_to_merge"
            state.error = None
            self._event(state, "verification_completed", result="passed")
        else:
            failed = next(item for item in results if item["status"] == "failed")
            state.status = "needs_revision"
            state.error = {
                "stage": "verification",
                "type": "CommandFailed",
                "message": (
                    f"{failed['name']} failed: "
                    f"{failed.get('stderr') or failed.get('stdout')}"
                ),
                "occurred_at": utc_now(),
                "recoverable_with": f"dev-pipeline revise --task-id {state.task_id}",
            }
            self._event(state, "verification_completed", result="failed")
        self.store.save_state(state)
        return state

    def _worktree_path(self, task_id: str) -> Path:
        safe_run = self.store.run_dir(task_id).resolve()
        expected = (self.worktree_root / task_id).resolve()
        if expected != safe_run:
            raise ValidationError("worktree_dir must resolve to the configured runs directory")
        return (expected / "worktree").resolve()

    @staticmethod
    def _require_status(state: RunState, expected: str) -> None:
        if state.status != expected:
            raise PipelineError(
                f"Run '{state.task_id}' is '{state.status}', expected '{expected}'",
                code="invalid_state_transition",
            )

    def _event(self, state: RunState, event: str, **details: Any) -> None:
        self.store.append_event(state.task_id, {"event": event, **details})

    def _git(
        self,
        arguments: list[str],
        *,
        cwd: Path,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if check and result.returncode != 0:
            self._raise_command_error(result, f"git {shlex.join(arguments)}")
        return result

    def _git_text(self, arguments: list[str], *, cwd: Path) -> str:
        return self._git(arguments, cwd=cwd).stdout.strip()

    def _raise_command_error(
        self,
        result: subprocess.CompletedProcess[str],
        label: str,
    ) -> None:
        raise PipelineError(
            f"{label} failed: {self._command_detail(result)}",
            code="git_command_failed",
        )

    @staticmethod
    def _command_detail(result: subprocess.CompletedProcess[str]) -> str:
        return (result.stderr or result.stdout).strip()[-4000:]

    @staticmethod
    def _decode_timeout(value: str | bytes | None) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")[-10_000:]
        return (value or "")[-10_000:]
