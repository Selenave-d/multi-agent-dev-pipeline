from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .agents import ModelAgent, RequirementAgent
from .errors import PipelineError, ValidationError
from .execution import WorktreeExecutor
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

COMMANDS = {"run", "approve", "reject", "status", "revise", "merge"}
TERMINAL_STATUSES = {"merged", "rejected"}


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


def add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default="config.json", help="Path to pipeline config JSON")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the multi-agent development pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Run requirement through analysis, development, review")
    add_config_argument(run)
    run.add_argument(
        "--requirement",
        required=True,
        help="Requirement file path or ZenTao reference such as story:123 / bug:123",
    )
    run.add_argument("--task-id", help="Task ID; defaults to requirement.task_id")
    run.add_argument("--resume", action="store_true", help="Resume a prior interrupted run")

    for name in ("approve", "reject", "revise", "merge"):
        command = subparsers.add_parser(name)
        add_config_argument(command)
        command.add_argument("--task-id", required=True)
    status = subparsers.add_parser("status")
    add_config_argument(status)
    status.add_argument("--all", action="store_true", help="Include merged and rejected tasks")
    return parser


def normalize_argv(argv: list[str] | None) -> list[str]:
    values = list(sys.argv[1:] if argv is None else argv)
    if values and values[0] not in COMMANDS and values[0] not in {"-h", "--help"}:
        values.insert(0, "run")
    return values


def load_runtime(args: argparse.Namespace) -> tuple[Path, dict[str, Any], RunStore]:
    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    pipeline_config = config.get("pipeline", {})
    runs_dir = resolve_runs_dir(config_path, pipeline_config.get("runs_dir", "runs"))
    return config_path, config, RunStore(runs_dir)


def build_executor(
    config_path: Path,
    config: dict[str, Any],
    store: RunStore,
) -> WorktreeExecutor:
    pipeline = config.get("pipeline", {})
    project_root = resolve_path(config_path, config.get("project", {}).get("root", "."))
    worktree_root = resolve_path(
        config_path,
        pipeline.get("worktree_dir", pipeline.get("runs_dir", "runs")),
    )
    commands = pipeline.get("commands", {})
    return WorktreeExecutor(
        store,
        project_root,
        worktree_root,
        {
            "lint": commands.get("lint", "npm run lint"),
            "test": commands.get("test", "npm run test"),
            "build": commands.get("build", "npm run build"),
        },
        command_timeout=int(pipeline.get("command_timeout_seconds", 1200)),
    )


def infer_requirement(
    config: dict[str, Any],
    reference: str,
) -> tuple[str, str | None]:
    requirement_config = config.get("providers", {}).get("requirement", {})
    is_legacy = isinstance(requirement_config, str)
    requirement_type = requirement_config if is_legacy else requirement_config.get("type", "file")
    if requirement_type == "file":
        resolved = str(Path(reference).resolve())
        return resolved, RunStore.read_json(Path(resolved)).get("task_id")
    if requirement_type == "zentao":
        return reference, ZenTaoRequirementSource.task_id_for(reference)
    return reference, None


def run_command(args: argparse.Namespace) -> dict[str, Any]:
    config_path, config, store = load_runtime(args)
    reference, inferred_task_id = infer_requirement(config, args.requirement)
    task_id = args.task_id or inferred_task_id
    if not task_id:
        raise ValidationError("Requirement must contain task_id or use --task-id")
    pipeline = config.get("pipeline", {})
    orchestrator = Orchestrator(
        store,
        build_agents(config, config_path),
        max_retries=int(pipeline.get("max_retries", 1)),
    )
    state = orchestrator.run(
        reference,
        task_id,
        resume=args.resume,
        metadata={"config_path": str(config_path)},
    )
    return state.to_dict()


def approve_command(args: argparse.Namespace) -> dict[str, Any]:
    config_path, config, store = load_runtime(args)
    state = store.load_or_create(args.task_id)
    review = store.load_artifact(state, "review")
    print(f"Task: {state.task_id}")
    print(f"Review: {review['review_result']}")
    print(f"Summary: {review['summary']}")
    print(f"Issues: {len(review['issues'])}")
    if input("Approve and apply this patch? [y/N] ").strip().lower() not in {"y", "yes"}:
        return {"task_id": state.task_id, "status": state.status, "confirmed": False}
    return build_executor(config_path, config, store).approve(state).to_dict()


def reject_command(args: argparse.Namespace) -> dict[str, Any]:
    config_path, config, store = load_runtime(args)
    state = store.load_or_create(args.task_id)
    return build_executor(config_path, config, store).reject(state).to_dict()


def revise_command(args: argparse.Namespace) -> dict[str, Any]:
    config_path, config, store = load_runtime(args)
    state = store.load_or_create(args.task_id)
    feedback = {"error": state.error}
    if "verification" in state.artifacts:
        feedback["verification"] = store.load_artifact(state, "verification")
    executor = build_executor(config_path, config, store)
    executor.cleanup_for_revision(state)
    pipeline = config.get("pipeline", {})
    orchestrator = Orchestrator(
        store,
        build_agents(config, config_path),
        max_retries=int(pipeline.get("max_retries", 1)),
    )
    return orchestrator.revise(args.task_id, feedback).to_dict()


def merge_command(args: argparse.Namespace) -> dict[str, Any]:
    config_path, config, store = load_runtime(args)
    state = store.load_or_create(args.task_id)
    return build_executor(config_path, config, store).merge(state).to_dict()


def status_command(args: argparse.Namespace) -> list[dict[str, Any]]:
    _, _, store = load_runtime(args)
    states = store.list_states()
    if not args.all:
        states = [state for state in states if state.status not in TERMINAL_STATUSES]
    rows = []
    for state in states:
        progress = f"{len(state.completed_stages)}/4"
        print(f"{state.task_id:<24} {state.status:<24} {progress:<6} {state.updated_at}")
        rows.append(
            {
                "task_id": state.task_id,
                "status": state.status,
                "progress": progress,
                "updated_at": state.updated_at,
            }
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(normalize_argv(argv))
    try:
        handlers = {
            "run": run_command,
            "approve": approve_command,
            "reject": reject_command,
            "status": status_command,
            "revise": revise_command,
            "merge": merge_command,
        }
        result = handlers[args.command](args)
        if args.command != "status":
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except PipelineError as exc:
        error = json.dumps({"error": exc.code, "message": str(exc)}, ensure_ascii=False)
        print(error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
