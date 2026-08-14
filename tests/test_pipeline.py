from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from dev_pipeline.agents import ModelAgent, RequirementAgent
from dev_pipeline.contracts import ARTIFACT_NAMES
from dev_pipeline.errors import PipelineError, StageExecutionError, ValidationError
from dev_pipeline.orchestrator import Orchestrator
from dev_pipeline.patches import PatchValidator
from dev_pipeline.providers import DemoModelClient, FileRequirementSource
from dev_pipeline.storage import RunStore

TASK_ID = "REQ-TEST-001"


def write_requirement(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "task_id": TASK_ID,
                "title": "添加搜索",
                "description": "列表支持关键词搜索",
                "priority": "high",
                "module": "用户",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def make_agents(model=None):
    model = model or DemoModelClient()
    return {
        "requirement": RequirementAgent(FileRequirementSource()),
        "analysis": ModelAgent("analysis", model),
        "development": ModelAgent("development", model),
        "review": ModelAgent("review", model),
    }


def test_pipeline_writes_valid_traceable_artifacts(tmp_path: Path) -> None:
    requirement = tmp_path / "requirement.json"
    write_requirement(requirement)
    store = RunStore(tmp_path / "runs")

    state = Orchestrator(store, make_agents()).run(str(requirement), TASK_ID)

    assert state.status == "awaiting_human_review"
    assert state.completed_stages == ["requirement", "analysis", "development", "review"]
    assert set(state.artifacts) == set(ARTIFACT_NAMES)
    assert all("sha256" in item for item in state.artifacts.values())
    review = store.load_artifact(state, "review")
    assert review["review_result"] == "pass_with_suggestions"
    assert review["task_id"] == TASK_ID
    events = store.read_events(TASK_ID)
    assert events[0]["event"] == "run_started"
    assert [event["stage"] for event in events if event["event"] == "stage_completed"] == [
        "requirement",
        "analysis",
        "development",
        "review",
    ]
    assert events[-1]["event"] == "run_completed"


def test_existing_run_requires_explicit_resume(tmp_path: Path) -> None:
    requirement = tmp_path / "requirement.json"
    write_requirement(requirement)
    orchestrator = Orchestrator(RunStore(tmp_path / "runs"), make_agents())
    orchestrator.run(str(requirement), TASK_ID)

    with pytest.raises(PipelineError, match="--resume"):
        orchestrator.run(str(requirement), TASK_ID)


class FailOnceModel(DemoModelClient):
    def __init__(self) -> None:
        self.failed = False

    def generate(self, stage, payload):
        if stage == "development" and not self.failed:
            self.failed = True
            raise RuntimeError("temporary model failure")
        return super().generate(stage, payload)


def test_failed_run_resumes_without_replaying_completed_stages(tmp_path: Path) -> None:
    requirement = tmp_path / "requirement.json"
    write_requirement(requirement)
    store = RunStore(tmp_path / "runs")
    model = FailOnceModel()
    orchestrator = Orchestrator(store, make_agents(model), max_retries=0)

    with pytest.raises(StageExecutionError, match="temporary model failure"):
        orchestrator.run(str(requirement), TASK_ID)

    failed = store.load_or_create(TASK_ID)
    assert failed.status == "failed"
    assert failed.completed_stages == ["requirement", "analysis"]
    requirement_attempts = failed.attempts["requirement"]

    recovered = orchestrator.run(str(requirement), TASK_ID, resume=True)
    assert recovered.status == "awaiting_human_review"
    assert recovered.attempts["requirement"] == requirement_attempts
    assert recovered.attempts["development"] == 2


def test_artifact_tampering_is_detected(tmp_path: Path) -> None:
    requirement = tmp_path / "requirement.json"
    write_requirement(requirement)
    store = RunStore(tmp_path / "runs")
    state = Orchestrator(store, make_agents()).run(str(requirement), TASK_ID)
    path = store.run_dir(TASK_ID) / ARTIFACT_NAMES["analysis"]
    path.write_text("{}", encoding="utf-8")

    with pytest.raises(ValidationError, match="Checksum mismatch"):
        store.load_artifact(state, "analysis")


def test_task_id_cannot_escape_runs_directory(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    with pytest.raises(ValidationError, match="task_id"):
        store.run_dir("../outside")


class RevisionModel(DemoModelClient):
    def __init__(self) -> None:
        self.feedback = None

    def generate(self, stage, payload):
        if stage == "development":
            self.feedback = payload.get("revision_feedback")
        return super().generate(stage, payload)


def test_revise_passes_failure_feedback_to_development_agent(tmp_path: Path) -> None:
    requirement = tmp_path / "requirement.json"
    write_requirement(requirement)
    store = RunStore(tmp_path / "runs")
    model = RevisionModel()
    orchestrator = Orchestrator(store, make_agents(model))
    state = orchestrator.run(str(requirement), TASK_ID)
    state.status = "needs_revision"
    state.error = {"stage": "verification", "message": "tests failed"}
    store.save_state(state)

    revised = orchestrator.revise(TASK_ID, {"error": state.error})

    assert revised.status == "awaiting_human_review"
    assert model.feedback == {"error": state.error}
    assert revised.attempts["development"] == 2
    assert revised.attempts["review"] == 2


class AlreadySatisfiedModel(DemoModelClient):
    def generate(self, stage, payload):
        if stage == "review":
            raise AssertionError("review must be skipped for already_satisfied development")
        result = super().generate(stage, payload)
        if stage == "analysis":
            result["analysis"]["change_status"] = "already_satisfied"
            result["analysis"]["changes"] = []
        if stage == "development":
            result["change_status"] = "already_satisfied"
            result["changes"] = []
            result["commit_message"] = "chore: no changes needed"
        return result


def test_already_satisfied_analysis_finishes_without_approval(tmp_path: Path) -> None:
    requirement = tmp_path / "requirement.json"
    write_requirement(requirement)
    store = RunStore(tmp_path / "runs")

    state = Orchestrator(store, make_agents(AlreadySatisfiedModel())).run(
        str(requirement), TASK_ID
    )

    assert state.status == "no_changes_needed"
    assert state.completed_stages == ["requirement", "analysis", "development"]
    assert "review" not in state.artifacts
    assert not (store.run_dir(TASK_ID) / ARTIFACT_NAMES["review"]).exists()
    development = store.load_artifact(state, "development")
    assert development["change_status"] == "already_satisfied"
    assert development["changes"] == []


def test_revise_skips_review_when_development_is_already_satisfied(tmp_path: Path) -> None:
    requirement = tmp_path / "requirement.json"
    write_requirement(requirement)
    store = RunStore(tmp_path / "runs")
    state = Orchestrator(store, make_agents()).run(str(requirement), TASK_ID)
    state.status = "needs_revision"
    store.save_state(state)

    revised = Orchestrator(store, make_agents(AlreadySatisfiedModel())).revise(
        TASK_ID, {"message": "reinspect current code"}
    )

    assert revised.status == "no_changes_needed"
    assert revised.completed_stages == ["requirement", "analysis", "development"]
    assert "review" not in revised.artifacts
    assert not (store.run_dir(TASK_ID) / ARTIFACT_NAMES["review"]).exists()


class InvalidThenValidModel(DemoModelClient):
    def __init__(self) -> None:
        self.development_calls = 0
        self.validation_feedback = None

    def generate(self, stage, payload):
        result = super().generate(stage, payload)
        if stage == "development":
            self.development_calls += 1
            self.validation_feedback = payload.get("development_validation_feedback")
            result["changes"][0]["diff"] = (
                "not a patch"
                if self.development_calls == 1
                else "--- a/app.txt\n+++ b/app.txt\n@@ -1 +1 @@\n-old\n+new\n"
            )
        return result


def make_git_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "core.autocrlf", "false"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    (path / "app.txt").write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.txt"], cwd=path, check=True, capture_output=True)
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


def test_invalid_patch_is_retried_and_only_valid_artifact_is_saved(tmp_path: Path) -> None:
    requirement = tmp_path / "requirement.json"
    write_requirement(requirement)
    project = tmp_path / "project"
    make_git_repo(project)
    store = RunStore(tmp_path / "runs")
    model = InvalidThenValidModel()
    orchestrator = Orchestrator(
        store,
        make_agents(model),
        max_retries=1,
        development_validator=PatchValidator(project),
    )

    state = orchestrator.run(str(requirement), TASK_ID)

    assert state.status == "awaiting_human_review"
    assert model.development_calls == 2
    assert model.validation_feedback is not None
    assert "git apply --3way" in model.validation_feedback["message"]
    saved = store.load_artifact(state, "development")
    assert saved["changes"][0]["diff"].startswith("--- a/app.txt")


class AlwaysInvalidPatchModel(DemoModelClient):
    def generate(self, stage, payload):
        result = super().generate(stage, payload)
        if stage == "development":
            result["changes"][0]["diff"] = "not a patch"
        return result


def test_invalid_patch_is_never_saved_when_all_attempts_fail(tmp_path: Path) -> None:
    requirement = tmp_path / "requirement.json"
    write_requirement(requirement)
    project = tmp_path / "project"
    make_git_repo(project)
    store = RunStore(tmp_path / "runs")
    orchestrator = Orchestrator(
        store,
        make_agents(AlwaysInvalidPatchModel()),
        max_retries=1,
        development_validator=PatchValidator(project),
    )

    with pytest.raises(StageExecutionError, match="git apply --3way"):
        orchestrator.run(str(requirement), TASK_ID)

    state = store.load_or_create(TASK_ID)
    assert state.status == "failed"
    assert state.error["stage"] == "development"
    assert "git apply --3way" in state.error["message"]
    assert "development" not in state.artifacts
    assert not (store.run_dir(TASK_ID) / "03_code_changes.json").exists()


def test_orchestrator_defaults_to_three_retries(tmp_path: Path) -> None:
    orchestrator = Orchestrator(RunStore(tmp_path / "runs"), make_agents())

    assert orchestrator.max_retries == 3
