from __future__ import annotations

import json
from pathlib import Path

from dev_pipeline.cli import main


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
