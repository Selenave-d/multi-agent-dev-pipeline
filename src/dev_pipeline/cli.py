from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .agents import ModelAgent, RequirementAgent
from .errors import PipelineError, ValidationError
from .orchestrator import Orchestrator, resolve_runs_dir
from .providers import DemoModelClient, FileRequirementSource
from .storage import RunStore


def load_config(path: Path) -> dict[str, Any]:
    config = RunStore.read_json(path)
    providers = config.get("providers", {})
    if providers.get("model") != "demo" or providers.get("requirement") != "file":
        raise ValidationError("MVP currently supports providers model='demo', requirement='file'")
    return config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the multi-agent development pipeline")
    parser.add_argument("--config", default="config.json", help="Path to pipeline config JSON")
    parser.add_argument("--requirement", required=True, help="Path to requirement JSON")
    parser.add_argument("--task-id", help="Task ID; defaults to requirement.task_id")
    parser.add_argument("--resume", action="store_true", help="Resume a prior interrupted run")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config_path = Path(args.config).resolve()
        requirement_path = Path(args.requirement).resolve()
        config = load_config(config_path)
        raw_requirement = RunStore.read_json(requirement_path)
        task_id = args.task_id or raw_requirement.get("task_id")
        if not task_id:
            raise ValidationError("Requirement must contain task_id or use --task-id")

        model = DemoModelClient()
        agents = {
            "requirement": RequirementAgent(FileRequirementSource()),
            "analysis": ModelAgent("analysis", model),
            "development": ModelAgent("development", model),
            "review": ModelAgent("review", model),
        }
        pipeline_config = config.get("pipeline", {})
        runs_dir = resolve_runs_dir(config_path, pipeline_config.get("runs_dir", "runs"))
        orchestrator = Orchestrator(
            RunStore(runs_dir),
            agents,
            max_retries=int(pipeline_config.get("max_retries", 1)),
        )
        state = orchestrator.run(str(requirement_path), task_id, resume=args.resume)
        print(json.dumps(state.to_dict(), ensure_ascii=False, indent=2))
        return 0
    except PipelineError as exc:
        error = json.dumps({"error": exc.code, "message": str(exc)}, ensure_ascii=False)
        print(error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
