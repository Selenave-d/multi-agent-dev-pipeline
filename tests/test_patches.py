from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from dev_pipeline.errors import PipelineError
from dev_pipeline.patches import PatchValidator


def make_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
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


def artifact(diff: str) -> dict:
    return {
        "task_id": "TASK-1",
        "change_status": "changes_required",
        "changes": [{"file": "app.txt", "diff": diff}],
        "commit_message": "fix: app",
    }


def test_patch_validator_accepts_applicable_diff_without_modifying_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    valid = "--- a/app.txt\n+++ b/app.txt\n@@ -1 +1 @@\n-old\n+new\n"

    PatchValidator(repo).validate(artifact(valid))

    assert (repo / "app.txt").read_text(encoding="utf-8") == "old\n"


def test_patch_validator_rejects_malformed_diff(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)

    with pytest.raises(PipelineError) as error:
        PatchValidator(repo).validate(artifact("not a patch"))

    assert error.value.code == "invalid_generated_patch"
    assert "git apply --check" in str(error.value)


def test_patch_validator_rejects_dirty_project(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    (repo / "app.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(PipelineError) as error:
        PatchValidator(repo).validate(artifact("not a patch"))

    assert error.value.code == "project_dirty"


def test_patch_validator_skips_legitimate_noop(tmp_path: Path) -> None:
    PatchValidator(tmp_path / "missing").validate(
        {
            "task_id": "TASK-1",
            "change_status": "already_satisfied",
            "changes": [],
            "commit_message": "chore: no changes needed",
        }
    )
