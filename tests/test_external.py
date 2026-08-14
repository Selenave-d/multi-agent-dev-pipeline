from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from dev_pipeline.agents import RequirementAgent
from dev_pipeline.development import DevelopmentWorkspace
from dev_pipeline.errors import PipelineError, ValidationError
from dev_pipeline.external import ExternalWorkflow
from dev_pipeline.formatting import DevelopmentFormatter
from dev_pipeline.patches import PatchValidator
from dev_pipeline.providers import FileRequirementSource
from dev_pipeline.storage import RunStore

TASK_ID = "HOST-1"


def make_git_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "core.autocrlf", "false"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    (path / "app.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "-m",
            "init",
        ],
        cwd=path,
        check=True,
        capture_output=True,
    )


def make_workflow(tmp_path: Path) -> tuple[ExternalWorkflow, RunStore, Path, Path]:
    project = tmp_path / "project"
    make_git_repo(project)
    requirement = tmp_path / "requirement.json"
    requirement.write_text(
        json.dumps(
            {
                "task_id": TASK_ID,
                "title": "调整值",
                "description": "把值改为 2",
                "priority": "medium",
                "module": "core",
            }
        ),
        encoding="utf-8",
    )
    store = RunStore(tmp_path / "runs")
    worktree = store.run_dir(TASK_ID) / "development-worktree"
    workflow = ExternalWorkflow(
        store,
        RequirementAgent(FileRequirementSource()),
        DevelopmentWorkspace(
            project,
            worktree,
            formatter=DevelopmentFormatter(project, auto_detect=False),
        ),
        PatchValidator(project),
        {"root": str(project), "name": "test"},
        ["app.py"],
    )
    return workflow, store, requirement, worktree


def analysis_artifact() -> dict:
    return {
        "task_id": TASK_ID,
        "analysis": {
            "change_status": "changes_required",
            "affected_files": ["app.py"],
            "changes": [
                {"file": "app.py", "action": "modify", "description": "调整值"}
            ],
            "dependencies": [],
            "estimated_complexity": "low",
            "assumptions": [],
        },
    }


def test_host_workflow_reaches_human_review_with_git_generated_patch(tmp_path: Path) -> None:
    workflow, store, requirement, worktree = make_workflow(tmp_path)

    state = workflow.start(str(requirement), TASK_ID)
    assert state.status == "awaiting_analysis"
    assert workflow.context(state, "analysis")["project_files"] == ["app.py"]

    state = workflow.submit(state, "analysis", analysis_artifact())
    assert state.status == "awaiting_development"
    state = workflow.prepare(state)
    assert state.status == "development_in_progress"
    assert worktree.is_dir()
    (worktree / "app.py").write_text("value = 2", encoding="utf-8")

    state = workflow.capture(
        state,
        {
            "task_id": TASK_ID,
            "change_status": "changes_required",
            "commit_message": "fix: adjust value",
        },
    )
    assert state.status == "awaiting_review"
    assert not worktree.exists()
    development = store.load_artifact(state, "development")
    assert "-value = 1" in development["changes"][0]["diff"]
    assert "+value = 2" in development["changes"][0]["diff"]

    review_context = workflow.context(state, "review")
    assert review_context["development"] == development
    state = workflow.submit(
        state,
        "review",
        {
            "task_id": TASK_ID,
            "review_result": "pass",
            "issues": [],
            "summary": "变更正确",
        },
    )

    assert state.status == "awaiting_human_review"
    assert state.completed_stages == ["requirement", "analysis", "development", "review"]


def test_host_workflow_rejects_wrong_stage_and_task_id(tmp_path: Path) -> None:
    workflow, _, requirement, _ = make_workflow(tmp_path)
    state = workflow.start(str(requirement), TASK_ID)

    with pytest.raises(PipelineError) as order_error:
        workflow.context(state, "review")
    assert order_error.value.code == "invalid_stage_order"

    artifact = analysis_artifact()
    artifact["task_id"] = "OTHER"
    with pytest.raises(ValidationError, match="does not match"):
        workflow.submit(state, "analysis", artifact)


def test_host_workflow_no_changes_skips_review(tmp_path: Path) -> None:
    workflow, _, requirement, worktree = make_workflow(tmp_path)
    state = workflow.start(str(requirement), TASK_ID)
    state = workflow.submit(state, "analysis", analysis_artifact())
    state = workflow.prepare(state)

    state = workflow.capture(
        state,
        {
            "task_id": TASK_ID,
            "change_status": "already_satisfied",
            "commit_message": "chore: no changes needed",
        },
    )

    assert state.status == "no_changes_needed"
    assert state.completed_stages == ["requirement", "analysis", "development"]
    assert not worktree.exists()


def test_host_revision_removes_stale_artifacts_and_exposes_feedback(tmp_path: Path) -> None:
    workflow, store, requirement, worktree = make_workflow(tmp_path)
    state = workflow.start(str(requirement), TASK_ID)
    state = workflow.submit(state, "analysis", analysis_artifact())
    state = workflow.prepare(state)
    (worktree / "app.py").write_text("value = 2\n", encoding="utf-8")
    state = workflow.capture(
        state,
        {
            "task_id": TASK_ID,
            "change_status": "changes_required",
            "commit_message": "fix: adjust value",
        },
    )
    state = workflow.submit(
        state,
        "review",
        {
            "task_id": TASK_ID,
            "review_result": "pass",
            "issues": [],
            "summary": "ok",
        },
    )
    state.status = "needs_revision"
    state.error = {"message": "test failed"}
    store.save_state(state)

    state = workflow.begin_revision(state, {"error": state.error})

    assert state.completed_stages == ["requirement", "analysis"]
    assert "development" not in state.artifacts
    assert "review" not in state.artifacts
    assert workflow.context(state, "development")["revision_feedback"] == {
        "error": {"message": "test failed"}
    }
