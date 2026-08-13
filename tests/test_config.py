from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.mark.parametrize("filename", ["config.demo.json", "config.example.json"])
def test_committed_config_templates_only_use_active_pipeline_fields(filename: str) -> None:
    project_root = Path(__file__).resolve().parents[1]
    config = json.loads((project_root / filename).read_text(encoding="utf-8"))

    pipeline = config["pipeline"]
    assert "stop_after" not in pipeline
    assert pipeline["worktree_dir"] == pipeline["runs_dir"]
