from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from dev_pipeline.contracts import RunState, utc_now
from dev_pipeline.errors import PipelineError
from dev_pipeline.execution import WorktreeExecutor
from dev_pipeline.storage import RunStore
from dev_pipeline.worklog import WorkLogResult

TASK_ID = "TASK-001"


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "project"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "core.autocrlf", "false")
    git(repo, "config", "user.name", "Pipeline Test")
    git(repo, "config", "user.email", "pipeline@example.test")
    (repo / ".gitignore").write_text("runs/\n", encoding="utf-8")
    (repo / "app.txt").write_text("old\n", encoding="utf-8")
    git(repo, "add", "--all")
    git(repo, "commit", "-m", "initial")
    return repo


def make_state(store: RunStore) -> RunState:
    state = RunState(
        TASK_ID,
        status="awaiting_human_review",
        completed_stages=["requirement", "analysis", "development", "review"],
    )
    store.save_artifact(
        state,
        "requirement",
        "01_requirement.json",
        {
            "task_id": TASK_ID,
            "title": "修复应用内容",
            "description": "更新应用内容",
            "priority": "medium",
            "module": "app",
            "raw_data": {"type": "bug"},
            "created_at": utc_now(),
            "errors": [],
        },
    )
    store.save_artifact(
        state,
        "development",
        "03_code_changes.json",
        {
            "task_id": TASK_ID,
            "change_status": "changes_required",
            "changes": [
                {
                    "file": "app.txt",
                    "diff": (
                        "--- a/app.txt\n"
                        "+++ b/app.txt\n"
                        "@@ -1 +1 @@\n"
                        "-old\n"
                        "+new\n"
                    ),
                }
            ],
            "commit_message": "feat: update app",
            "created_at": utc_now(),
            "errors": [],
        },
    )
    store.save_artifact(
        state,
        "review",
        "04_review.json",
        {
            "task_id": TASK_ID,
            "review_result": "pass",
            "issues": [],
            "summary": "ready",
            "created_at": utc_now(),
            "errors": [],
        },
    )
    store.save_state(state)
    return state


def python_command(source: str) -> str:
    return f'"{sys.executable}" -c "{source}"'


class FakeBrowserVerifier:
    def __init__(self, status: str = "passed") -> None:
        self.status = status
        self.calls: list[tuple[str, Path]] = []

    def verify(self, task_id: str, worktree: Path) -> dict[str, object]:
        self.calls.append((task_id, worktree))
        return {
            "name": "browser",
            "command": "external browser",
            "status": self.status,
            "exit_code": 0 if self.status == "passed" else 1,
            "duration_seconds": 0.1,
            "stdout": "browser output",
            "stderr": "" if self.status == "passed" else "page assertion failed",
            "scenarios": [],
        }


class RecordingWorkLogSink:
    def __init__(self, result: WorkLogResult | None = None) -> None:
        self.result = result or WorkLogResult(
            status="written",
            path="daily.md",
            entry="- [09:07] 修复: 应用内容",
            reason="configured",
        )
        self.requirements: list[dict[str, object]] = []

    def write(self, requirement: dict[str, object]) -> WorkLogResult:
        self.requirements.append(requirement)
        return self.result


class FailingWorkLogSink:
    def write(self, requirement: dict[str, object]) -> WorkLogResult:
        raise OSError("Obsidian directory is read-only")


def test_approve_applies_patch_verifies_and_merges(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    store = RunStore(repo / "runs")
    state = make_state(store)
    command = python_command("print('ok')")
    executor = WorktreeExecutor(
        store,
        repo,
        repo / "runs",
        {"lint": command, "test": command, "build": command},
    )

    approved = executor.approve(state)

    assert approved.status == "ready_to_merge"
    worktree = Path(approved.metadata["worktree_path"])
    assert (worktree / "app.txt").read_text(encoding="utf-8") == "new\n"
    verification = store.load_artifact(approved, "verification")
    assert verification["result"] == "passed"
    assert [step["status"] for step in verification["steps"]] == [
        "passed",
        "passed",
        "passed",
    ]
    base_commit = git(repo, "rev-parse", "HEAD")

    merged = executor.merge(approved)

    assert merged.status == "merged"
    assert (repo / "app.txt").read_text(encoding="utf-8") == "new\n"
    assert git(repo, "rev-parse", "HEAD") != base_commit
    assert git(repo, "log", "-1", "--pretty=%s") == f"merge: {TASK_ID}"


def test_verification_failure_needs_revision(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    store = RunStore(repo / "runs")
    state = make_state(store)
    executor = WorktreeExecutor(
        store,
        repo,
        repo / "runs",
        {
            "lint": python_command("import sys; print('lint failed'); sys.exit(3)"),
            "test": python_command("print('should not run')"),
            "build": python_command("print('should not run')"),
        },
    )

    failed = executor.approve(state)

    assert failed.status == "needs_revision"
    assert failed.error["stage"] == "verification"
    verification = store.load_artifact(failed, "verification")
    assert verification["result"] == "failed"
    assert len(verification["steps"]) == 1
    assert verification["steps"][0]["exit_code"] == 3


def test_merge_records_written_worklog_event(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    store = RunStore(repo / "runs")
    state = make_state(store)
    work_log = RecordingWorkLogSink()
    executor = WorktreeExecutor(
        store,
        repo,
        repo / "runs",
        {},
        work_log_sink=work_log,
    )

    merged = executor.merge(executor.approve(state))

    assert merged.status == "merged"
    assert work_log.requirements[0]["title"] == "修复应用内容"
    assert store.read_events(TASK_ID)[-1]["event"] == "work_log_written"


def test_merge_worklog_failure_does_not_change_merged_status(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    store = RunStore(repo / "runs")
    state = make_state(store)
    executor = WorktreeExecutor(
        store,
        repo,
        repo / "runs",
        {},
        work_log_sink=FailingWorkLogSink(),
    )

    merged = executor.merge(executor.approve(state))

    assert merged.status == "merged"
    persisted = store.load_or_create(TASK_ID)
    assert persisted.status == "merged"
    assert store.read_events(TASK_ID)[-1]["event"] == "work_log_failed"


def test_merge_records_worklog_skip(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    store = RunStore(repo / "runs")
    state = make_state(store)
    work_log = RecordingWorkLogSink(
        WorkLogResult(status="skipped", reason="path_not_found")
    )
    executor = WorktreeExecutor(
        store,
        repo,
        repo / "runs",
        {},
        work_log_sink=work_log,
    )

    merged = executor.merge(executor.approve(state))

    assert merged.status == "merged"
    event = store.read_events(TASK_ID)[-1]
    assert event["event"] == "work_log_skipped"
    assert event["reason"] == "path_not_found"


def test_browser_verification_runs_after_commands_pass(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    store = RunStore(repo / "runs")
    state = make_state(store)
    browser = FakeBrowserVerifier()
    executor = WorktreeExecutor(
        store,
        repo,
        repo / "runs",
        {},
        browser_verifier=browser,
    )

    approved = executor.approve(state)

    assert approved.status == "ready_to_merge"
    assert browser.calls == [(TASK_ID, Path(approved.metadata["worktree_path"]))]
    verification = store.load_artifact(approved, "verification")
    assert [step["name"] for step in verification["steps"]] == [
        "lint",
        "test",
        "build",
        "browser",
    ]


def test_browser_failure_needs_revision(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    store = RunStore(repo / "runs")
    state = make_state(store)
    browser = FakeBrowserVerifier("failed")
    executor = WorktreeExecutor(
        store,
        repo,
        repo / "runs",
        {},
        browser_verifier=browser,
    )

    failed = executor.approve(state)

    assert failed.status == "needs_revision"
    assert failed.error["stage"] == "verification"
    assert "browser failed" in failed.error["message"]


def test_browser_does_not_run_after_command_failure(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    store = RunStore(repo / "runs")
    state = make_state(store)
    browser = FakeBrowserVerifier()
    executor = WorktreeExecutor(
        store,
        repo,
        repo / "runs",
        {"lint": python_command("import sys; sys.exit(1)")},
        browser_verifier=browser,
    )

    failed = executor.approve(state)

    assert failed.status == "needs_revision"
    assert browser.calls == []


def test_reject_records_decision_without_worktree(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    store = RunStore(repo / "runs")
    state = make_state(store)
    executor = WorktreeExecutor(store, repo, repo / "runs", {})

    rejected = executor.reject(state)

    assert rejected.status == "rejected"
    assert not (store.run_dir(TASK_ID) / "worktree").exists()
    assert store.load_artifact(rejected, "decision")["decision"] == "reject"


def test_merge_refuses_advanced_main_branch(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    store = RunStore(repo / "runs")
    state = make_state(store)
    executor = WorktreeExecutor(store, repo, repo / "runs", {})
    approved = executor.approve(state)
    assert approved.status == "ready_to_merge"
    (repo / "other.txt").write_text("advance\n", encoding="utf-8")
    git(repo, "add", "other.txt")
    git(repo, "commit", "-m", "advance main")

    with pytest.raises(PipelineError, match="advanced after approval"):
        executor.merge(approved)


def test_revision_cleanup_refuses_ready_to_merge_task(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    store = RunStore(repo / "runs")
    state = make_state(store)
    executor = WorktreeExecutor(store, repo, repo / "runs", {})
    approved = executor.approve(state)
    worktree = Path(approved.metadata["worktree_path"])
    assert approved.status == "ready_to_merge"

    with pytest.raises(PipelineError, match="expected 'needs_revision'"):
        executor.cleanup_for_revision(approved)

    assert worktree.is_dir()


def test_old_state_without_metadata_uses_default(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    path = store.state_path("OLD-1")
    path.write_text(
        json.dumps(
            {
                "task_id": "OLD-1",
                "status": "awaiting_human_review",
                "current_stage": None,
                "completed_stages": [],
                "attempts": {},
                "artifacts": {},
                "error": None,
                "created_at": utc_now(),
                "updated_at": utc_now(),
            }
        ),
        encoding="utf-8",
    )

    assert store.load_or_create("OLD-1").metadata == {}
