from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from dev_pipeline.cli import (
    build_agents,
    build_development_validator,
    build_executor,
    build_parser,
    logs_command,
    main,
)
from dev_pipeline.errors import PipelineError
from dev_pipeline.providers import ZenTaoRequirementSource
from dev_pipeline.storage import RunStore
from dev_pipeline.worklog import WorkLogWriter


def test_cli_end_to_end(tmp_path: Path, capsys) -> None:
    requirement = tmp_path / "requirement.json"
    requirement.write_text(
        json.dumps(
            {
                "task_id": "REQ-CLI-001",
                "title": "搜索",
                "description": "添加搜索功能",
                "priority": "high",
                "module": "用户",
            }
        ),
        encoding="utf-8",
    )
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "pipeline": {"runs_dir": "runs", "max_retries": 1},
                "providers": {"model": "demo", "requirement": "file"},
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(["--config", str(config), "--requirement", str(requirement)])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "awaiting_human_review"
    assert (tmp_path / "runs" / "REQ-CLI-001" / "04_review.json").is_file()


def test_status_lists_non_terminal_tasks(tmp_path: Path, capsys) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "pipeline": {"runs_dir": "runs"},
                "providers": {"model": "demo", "requirement": "file"},
            }
        ),
        encoding="utf-8",
    )
    runs = tmp_path / "runs"
    for task_id, status in (("ACTIVE-1", "awaiting_human_review"), ("DONE-1", "merged")):
        task = runs / task_id
        task.mkdir(parents=True)
        (task / "run_state.json").write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "status": status,
                    "current_stage": None,
                    "completed_stages": ["requirement"],
                    "attempts": {},
                    "artifacts": {},
                    "error": None,
                    "metadata": {},
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )

    assert main(["status", "--config", str(config)]) == 0
    output = capsys.readouterr().out
    assert "ACTIVE-1" in output
    assert "1/4" in output
    assert "DONE-1" not in output


def test_logs_prints_task_events(tmp_path: Path, capsys) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "pipeline": {"runs_dir": "runs"},
                "providers": {"model": "demo", "requirement": "file"},
            }
        ),
        encoding="utf-8",
    )
    task = tmp_path / "runs" / "TASK-1"
    task.mkdir(parents=True)
    (task / "events.jsonl").write_text(
        json.dumps({"timestamp": "now", "event": "stage_started", "stage": "analysis"})
        + "\n",
        encoding="utf-8",
    )

    assert main(["logs", "--config", str(config), "--task-id", "TASK-1"]) == 0
    output = capsys.readouterr().out
    assert '"event": "stage_started"' in output
    assert '"stage": "analysis"' in output


def test_logs_rejects_unknown_task(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "pipeline": {"runs_dir": "runs"},
                "providers": {"model": "demo", "requirement": "file"},
            }
        ),
        encoding="utf-8",
    )
    args = build_parser().parse_args(
        ["logs", "--config", str(config), "--task-id", "MISSING"]
    )

    with pytest.raises(PipelineError) as error:
        logs_command(args)

    assert error.value.code == "run_not_found"


def test_build_agents_injects_expected_zentao_product(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config = {
        "project": {"root": ".", "zentao_product": "DTS"},
        "providers": {
            "requirement": {"type": "zentao", "config_file": "zentao.json"},
            "analysis": {"type": "demo"},
            "development": {"type": "demo"},
            "review": {"type": "demo"},
        },
    }

    agents = build_agents(config, config_path)

    source = agents["requirement"].source
    assert isinstance(source, ZenTaoRequirementSource)
    assert source.expected_product_code == "DTS"


def test_legacy_demo_config_does_not_enable_patch_validation(tmp_path: Path) -> None:
    config = {"providers": {"model": "demo", "requirement": "file"}}

    assert build_development_validator(config, tmp_path / "config.json") is None


def test_build_executor_validates_enabled_browser_config(tmp_path: Path) -> None:
    config = {
        "project": {"root": "."},
        "pipeline": {"browser": {"enabled": True, "start_command": "npm run serve"}},
    }

    with pytest.raises(PipelineError, match="base_url"):
        build_executor(tmp_path / "config.json", config, RunStore(tmp_path / "runs"))


def test_build_executor_configures_relative_worklog_path(tmp_path: Path) -> None:
    config = {
        "project": {"root": "."},
        "pipeline": {
            "runs_dir": "runs",
            "worktree_dir": "runs",
            "work_log": {"enabled": True, "path": "daily-logs"},
        },
    }

    executor = build_executor(
        tmp_path / "config.json",
        config,
        RunStore(tmp_path / "runs"),
    )

    assert isinstance(executor.work_log_sink, WorkLogWriter)
    assert executor.work_log_sink.configured_path == (tmp_path / "daily-logs").resolve()


def test_cli_discovers_project_local_config_for_host_start(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    requirement = tmp_path / "requirement.json"
    requirement.write_text(
        json.dumps(
            {
                "task_id": "HOST-CLI-1",
                "title": "调整页面",
                "description": "更新展示内容",
                "priority": "medium",
                "module": "page",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / ".dev-pipeline.json").write_text(
        json.dumps(
            {
                "project": {"root": ".", "name": "host-test"},
                "pipeline": {"runs_dir": "outside-runs"},
                "providers": {
                    "requirement": {"type": "file"},
                    "analysis": {"type": "demo"},
                    "development": {"type": "demo"},
                    "review": {"type": "demo"},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    assert main(["start", "--requirement", str(requirement)]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["task_id"] == "HOST-CLI-1"
    assert output["status"] == "awaiting_analysis"


def test_cli_host_stage_protocol_reaches_human_review(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("value = 1\n", encoding="utf-8")
    config = project / ".dev-pipeline.json"
    config.write_text(
        json.dumps(
            {
                "project": {"root": ".", "name": "host-cli"},
                "pipeline": {
                    "runs_dir": str(tmp_path / "runs"),
                    "worktree_dir": str(tmp_path / "runs"),
                    "commands": {"format": ""},
                },
                "providers": {"requirement": {"type": "file"}},
            }
        ),
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-b", "main"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "add", "--all"], cwd=project, check=True, capture_output=True)
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
        cwd=project,
        check=True,
        capture_output=True,
    )
    requirement = tmp_path / "requirement.json"
    requirement.write_text(
        json.dumps(
            {
                "task_id": "HOST-CLI-2",
                "title": "调整值",
                "description": "把值改为 2",
                "priority": "medium",
                "module": "core",
            }
        ),
        encoding="utf-8",
    )
    analysis = tmp_path / "analysis.json"
    analysis.write_text(
        json.dumps(
            {
                "task_id": "HOST-CLI-2",
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
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(project)

    assert main(["start", "--requirement", str(requirement)]) == 0
    capsys.readouterr()
    assert main(
        [
            "submit",
            "--task-id",
            "HOST-CLI-2",
            "--stage",
            "analysis",
            "--artifact",
            str(analysis),
        ]
    ) == 0
    capsys.readouterr()
    assert main(["prepare", "--task-id", "HOST-CLI-2"]) == 0
    prepared = json.loads(capsys.readouterr().out)
    worktree = Path(prepared["metadata"]["development_worktree_path"])
    (worktree / "app.py").write_text("value = 2\n", encoding="utf-8")
    result = tmp_path / "development-result.json"
    result.write_text(
        json.dumps(
            {
                "task_id": "HOST-CLI-2",
                "change_status": "changes_required",
                "commit_message": "fix: adjust value",
            }
        ),
        encoding="utf-8",
    )
    assert main(["capture", "--task-id", "HOST-CLI-2", "--result", str(result)]) == 0
    captured = json.loads(capsys.readouterr().out)
    assert captured["status"] == "awaiting_review"
    assert main(["context", "--task-id", "HOST-CLI-2", "--stage", "review"]) == 0
    review_context = json.loads(capsys.readouterr().out)
    assert review_context["development"]["changes"]
    review = tmp_path / "review.json"
    review.write_text(
        json.dumps(
            {
                "task_id": "HOST-CLI-2",
                "review_result": "pass",
                "issues": [],
                "summary": "变更正确",
            }
        ),
        encoding="utf-8",
    )
    assert main(
        [
            "submit",
            "--task-id",
            "HOST-CLI-2",
            "--stage",
            "review",
            "--artifact",
            str(review),
        ]
    ) == 0
    completed = json.loads(capsys.readouterr().out)
    assert completed["status"] == "awaiting_human_review"
