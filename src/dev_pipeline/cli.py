from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .agents import ModelAgent, RequirementAgent
from .errors import PipelineError, ValidationError
from .orchestrator import Orchestrator, resolve_runs_dir
from .providers import (
    ClaudeCodeClient,
    DemoModelClient,
    FileRequirementSource,
    KimiCodeClient,
    ProjectContext,
    ReviewClient,
    ZenTaoRequirementSource,
)
from .storage import RunStore


def load_config(path: Path) -> dict[str, Any]:
    config = RunStore.read_json(path)
    providers = config.get("providers", {})
    if not isinstance(providers, dict):
        raise ValidationError("'providers' must be a JSON object")
    return config


def resolve_path(config_path: Path, configured: str) -> Path:
    path = Path(configured).expanduser()
    return path.resolve() if path.is_absolute() else (config_path.parent / path).resolve()


def build_agents(config: dict[str, Any], config_path: Path) -> dict[str, Any]:
    providers = config.get("providers", {})
    legacy_model = providers.get("model")
    if legacy_model:
        if legacy_model != "demo" or providers.get("requirement") != "file":
            raise ValidationError(
                "Legacy provider config only supports model='demo', requirement='file'"
            )
        demo = DemoModelClient()
        return {
            "requirement": RequirementAgent(FileRequirementSource()),
            "analysis": ModelAgent("analysis", demo),
            "development": ModelAgent("development", demo),
            "review": ModelAgent("review", demo),
        }

    project_config = config.get("project", {})
    project_root = resolve_path(config_path, project_config.get("root", "."))
    project = ProjectContext(project_root, project_config)

    requirement_config = providers.get("requirement", {"type": "file"})
    requirement_type = requirement_config.get("type", "file")
    if requirement_type == "file":
        requirement_source = FileRequirementSource()
    elif requirement_type == "zentao":
        configured_path = requirement_config.get("config_file")
        config_file = resolve_path(config_path, configured_path) if configured_path else None
        requirement_source = ZenTaoRequirementSource(
            config_file,
            timeout=int(requirement_config.get("timeout_seconds", 30)),
        )
    else:
        raise ValidationError(f"Unsupported requirement provider: {requirement_type}")

    analysis_config = providers.get("analysis", {"type": "demo"})
    development_config = providers.get("development", {"type": "demo"})
    review_config = providers.get("review", {"type": "demo"})
    demo = DemoModelClient()

    analysis_type = analysis_config.get("type", "demo")
    if analysis_type == "demo":
        analysis_client = demo
    elif analysis_type == "kimi":
        analysis_client = KimiCodeClient(
            project,
            command=analysis_config.get("command", "kimi"),
            model=analysis_config.get("model"),
            timeout=int(analysis_config.get("timeout_seconds", 600)),
        )
    else:
        raise ValidationError(f"Unsupported analysis provider: {analysis_type}")

    development_type = development_config.get("type", "demo")
    if development_type == "demo":
        development_client = demo
    elif development_type == "claude":
        development_client = ClaudeCodeClient(
            project,
            command=development_config.get("command", "claude"),
            model=development_config.get("model"),
            timeout=int(development_config.get("timeout_seconds", 900)),
        )
    else:
        raise ValidationError(f"Unsupported development provider: {development_type}")

    review_type = review_config.get("type", "demo")
    if review_type == "demo":
        review_client = demo
    elif review_type in {"kimi", "claude", "codex"}:
        review_client = ReviewClient(
            project,
            tool=review_type,
            command=review_config.get("command", review_type),
            model=review_config.get("model"),
            timeout=int(review_config.get("timeout_seconds", 600)),
        )
    else:
        raise ValidationError(f"Unsupported review provider: {review_type}")

    return {
        "requirement": RequirementAgent(requirement_source),
        "analysis": ModelAgent("analysis", analysis_client),
        "development": ModelAgent("development", development_client),
        "review": ModelAgent("review", review_client),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the multi-agent development pipeline")
    parser.add_argument("--config", default="config.json", help="Path to pipeline config JSON")
    parser.add_argument(
        "--requirement",
        required=True,
        help="Requirement file path or ZenTao reference such as story:123 / bug:123",
    )
    parser.add_argument("--task-id", help="Task ID; defaults to requirement.task_id")
    parser.add_argument("--resume", action="store_true", help="Resume a prior interrupted run")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config_path = Path(args.config).resolve()
        config = load_config(config_path)
        providers = config.get("providers", {})
        requirement_config = providers.get("requirement", {})
        is_legacy = isinstance(requirement_config, str)
        requirement_type = (
            requirement_config if is_legacy else requirement_config.get("type", "file")
        )
        reference = args.requirement
        if requirement_type == "file":
            reference = str(Path(reference).resolve())
            raw_requirement = RunStore.read_json(Path(reference))
            inferred_task_id = raw_requirement.get("task_id")
        elif requirement_type == "zentao":
            inferred_task_id = ZenTaoRequirementSource.task_id_for(reference)
        else:
            inferred_task_id = None
        task_id = args.task_id or inferred_task_id
        if not task_id:
            raise ValidationError("Requirement must contain task_id or use --task-id")

        agents = build_agents(config, config_path)
        pipeline_config = config.get("pipeline", {})
        runs_dir = resolve_runs_dir(config_path, pipeline_config.get("runs_dir", "runs"))
        orchestrator = Orchestrator(
            RunStore(runs_dir),
            agents,
            max_retries=int(pipeline_config.get("max_retries", 1)),
        )
        state = orchestrator.run(reference, task_id, resume=args.resume)
        print(json.dumps(state.to_dict(), ensure_ascii=False, indent=2))
        return 0
    except PipelineError as exc:
        error = json.dumps({"error": exc.code, "message": str(exc)}, ensure_ascii=False)
        print(error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
