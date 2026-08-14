from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from .agents import ModelAgent, RequirementAgent
from .browser import PlaywrightBrowserVerifier
from .development import DevelopmentWorkspace
from .errors import PipelineError, ValidationError
from .execution import WorktreeExecutor
from .external import ExternalWorkflow
from .formatting import DevelopmentFormatter
from .orchestrator import Orchestrator, resolve_runs_dir
from .patches import PatchValidator
from .providers import (
    ClaudeCodeClient,
    DemoModelClient,
    FileRequirementSource,
    KimiCodeClient,
    ProjectContext,
    ReviewClient,
    SubprocessCommandRunner,
    ZenTaoRequirementSource,
)
from .storage import RunStore
from .worklog import WorkLogWriter

COMMANDS = {
    "run",
    "start",
    "context",
    "submit",
    "prepare",
    "capture",
    "approve",
    "reject",
    "status",
    "logs",
    "revise",
    "merge",
}
TERMINAL_STATUSES = {"merged", "rejected", "no_changes_needed"}


def load_config(path: Path) -> dict[str, Any]:
    config = RunStore.read_json(path)
    providers = config.get("providers", {})
    if not isinstance(providers, dict):
        raise ValidationError("'providers' must be a JSON object")
    return config


def resolve_path(config_path: Path, configured: str) -> Path:
    path = Path(configured).expanduser()
    return path.resolve() if path.is_absolute() else (config_path.parent / path).resolve()


def build_requirement_agent(
    config: dict[str, Any], config_path: Path
) -> RequirementAgent:
    providers = config.get("providers", {})
    project_config = config.get("project", {})
    requirement_config = providers.get("requirement", {"type": "file"})
    if isinstance(requirement_config, str):
        requirement_type = requirement_config
        requirement_config = {"type": requirement_type}
    else:
        requirement_type = requirement_config.get("type", "file")
    if requirement_type == "file":
        return RequirementAgent(FileRequirementSource())
    if requirement_type == "zentao":
        configured_path = requirement_config.get("config_file")
        config_file = resolve_path(config_path, configured_path) if configured_path else None
        return RequirementAgent(
            ZenTaoRequirementSource(
                config_file,
                expected_product_code=project_config.get("zentao_product"),
                timeout=int(requirement_config.get("timeout_seconds", 30)),
            )
        )
    raise ValidationError(f"Unsupported requirement provider: {requirement_type}")


def build_agents(
    config: dict[str, Any],
    config_path: Path,
    *,
    store: RunStore | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
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
    pipeline_config = config.get("pipeline", {})
    pipeline_commands = pipeline_config.get("commands", {})

    def observer(stage: str, tool: str):
        if not store or not task_id:
            return None

        def record(event: dict[str, Any]) -> None:
            details = dict(event)
            stdout = str(details.pop("stdout", ""))
            stderr = str(details.pop("stderr", ""))
            if stdout or stderr:
                details["output"] = store.save_tool_output(
                    task_id,
                    stage,
                    tool,
                    stdout=stdout,
                    stderr=stderr,
                )
            store.append_event(task_id, {"stage": stage, **details})

        return record

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
            runner=SubprocessCommandRunner(observer("analysis", "kimi")),
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
        format_is_configured = "format" in pipeline_commands
        development_client = ClaudeCodeClient(
            project,
            runner=SubprocessCommandRunner(observer("development", "claude")),
            worktree_path=(store.run_dir(task_id) / "development-worktree")
            if store and task_id
            else None,
            event_callback=observer("development", "git"),
            formatter=DevelopmentFormatter(
                project_root,
                command=pipeline_commands.get("format"),
                auto_detect=not format_is_configured,
                timeout=int(pipeline_config.get("format_timeout_seconds", 300)),
            ),
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
            runner=SubprocessCommandRunner(observer("review", review_type)),
            tool=review_type,
            command=review_config.get("command", review_type),
            model=review_config.get("model"),
            timeout=int(review_config.get("timeout_seconds", 600)),
        )
    else:
        raise ValidationError(f"Unsupported review provider: {review_type}")

    return {
        "requirement": build_requirement_agent(config, config_path),
        "analysis": ModelAgent("analysis", analysis_client),
        "development": ModelAgent("development", development_client),
        "review": ModelAgent("review", review_client),
    }


def add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        help="Config JSON; defaults to .dev-pipeline.json or config.json in the current directory",
    )


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

    start = subparsers.add_parser("start", help="Start a host-Agent run and fetch requirement")
    add_config_argument(start)
    start.add_argument("--requirement", required=True)
    start.add_argument("--task-id", help="Task ID; defaults to requirement.task_id")

    context = subparsers.add_parser("context", help="Print context for the pending host stage")
    add_config_argument(context)
    context.add_argument("--task-id", required=True)
    context.add_argument("--stage", required=True, choices=("analysis", "development", "review"))

    submit = subparsers.add_parser("submit", help="Submit a host analysis or review artifact")
    add_config_argument(submit)
    submit.add_argument("--task-id", required=True)
    submit.add_argument("--stage", required=True, choices=("analysis", "review"))
    submit.add_argument("--artifact", required=True, help="Path to artifact JSON")

    prepare = subparsers.add_parser("prepare", help="Create the host development worktree")
    add_config_argument(prepare)
    prepare.add_argument("--task-id", required=True)

    capture = subparsers.add_parser("capture", help="Capture host edits as a validated Git patch")
    add_config_argument(capture)
    capture.add_argument("--task-id", required=True)
    capture.add_argument("--result", required=True, help="Development result JSON path")

    for name in ("approve", "reject", "revise", "merge"):
        command = subparsers.add_parser(name)
        add_config_argument(command)
        command.add_argument("--task-id", required=True)
    status = subparsers.add_parser("status")
    add_config_argument(status)
    status.add_argument("--all", action="store_true", help="Include merged and rejected tasks")
    logs = subparsers.add_parser("logs", help="Show task execution events")
    add_config_argument(logs)
    logs.add_argument("--task-id", required=True)
    logs.add_argument("--follow", action="store_true", help="Wait for new events until terminal")
    return parser


def normalize_argv(argv: list[str] | None) -> list[str]:
    values = list(sys.argv[1:] if argv is None else argv)
    if values and values[0] not in COMMANDS and values[0] not in {"-h", "--help"}:
        values.insert(0, "run")
    return values


def load_runtime(args: argparse.Namespace) -> tuple[Path, dict[str, Any], RunStore]:
    configured = getattr(args, "config", None)
    if configured:
        config_path = Path(configured).resolve()
    else:
        candidates = (Path.cwd() / ".dev-pipeline.json", Path.cwd() / "config.json")
        config_path = next((path.resolve() for path in candidates if path.is_file()), None)
        if config_path is None:
            raise ValidationError(
                "No config found; create .dev-pipeline.json in the current project or pass --config"
            )
    config = load_config(config_path)
    pipeline_config = config.get("pipeline", {})
    runs_dir = resolve_runs_dir(config_path, pipeline_config.get("runs_dir", "runs"))
    return config_path, config, RunStore(runs_dir)


def load_existing_state(store: RunStore, task_id: str) -> Any:
    if not store.state_path(task_id).is_file():
        raise PipelineError(f"Run '{task_id}' does not exist", code="run_not_found")
    return store.load_or_create(task_id)


def build_external_workflow(
    config_path: Path,
    config: dict[str, Any],
    store: RunStore,
    task_id: str,
) -> ExternalWorkflow:
    project_config = config.get("project", {})
    project_root = resolve_path(config_path, project_config.get("root", "."))
    pipeline = config.get("pipeline", {})
    commands = pipeline.get("commands", {})
    project = ProjectContext(project_root, project_config)
    workspace = DevelopmentWorkspace(
        project_root,
        store.run_dir(task_id) / "development-worktree",
        formatter=DevelopmentFormatter(
            project_root,
            command=commands.get("format"),
            auto_detect="format" not in commands,
            timeout=int(pipeline.get("format_timeout_seconds", 300)),
        ),
        event_callback=lambda event: store.append_event(task_id, event),
    )
    return ExternalWorkflow(
        store,
        build_requirement_agent(config, config_path),
        workspace,
        PatchValidator(project_root),
        project_config,
        project.tree(),
    )


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
    browser_config = pipeline.get("browser", {})
    if not isinstance(browser_config, dict):
        raise ValidationError("'pipeline.browser' must be a JSON object")
    browser_verifier = None
    if browser_config.get("enabled", False):
        browser_verifier = PlaywrightBrowserVerifier(store, browser_config)
    work_log_config = pipeline.get("work_log", {})
    if not isinstance(work_log_config, dict):
        raise ValidationError("'pipeline.work_log' must be a JSON object")
    configured_work_log_path = work_log_config.get("path")
    if configured_work_log_path is not None and not isinstance(configured_work_log_path, str):
        raise ValidationError("'pipeline.work_log.path' must be a string")
    work_log_path = (
        resolve_path(config_path, configured_work_log_path)
        if configured_work_log_path
        else None
    )
    work_log_sink = WorkLogWriter(
        project_root,
        configured_path=work_log_path,
        enabled=bool(work_log_config.get("enabled", True)),
    )
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
        browser_verifier=browser_verifier,
        work_log_sink=work_log_sink,
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


def build_development_validator(
    config: dict[str, Any],
    config_path: Path,
) -> PatchValidator | None:
    providers = config.get("providers", {})
    if providers.get("model") == "demo":
        return None
    development_type = providers.get("development", {}).get("type", "demo")
    if development_type == "demo":
        return None
    project_root = resolve_path(config_path, config.get("project", {}).get("root", "."))
    return PatchValidator(project_root)


def run_command(args: argparse.Namespace) -> dict[str, Any]:
    config_path, config, store = load_runtime(args)
    reference, inferred_task_id = infer_requirement(config, args.requirement)
    task_id = args.task_id or inferred_task_id
    if not task_id:
        raise ValidationError("Requirement must contain task_id or use --task-id")
    pipeline = config.get("pipeline", {})
    orchestrator = Orchestrator(
        store,
        build_agents(config, config_path, store=store, task_id=task_id),
        max_retries=int(pipeline.get("max_retries", 3)),
        development_validator=build_development_validator(config, config_path),
    )
    state = orchestrator.run(
        reference,
        task_id,
        resume=args.resume,
        metadata={"config_path": str(config_path)},
    )
    return state.to_dict()


def start_command(args: argparse.Namespace) -> dict[str, Any]:
    config_path, config, store = load_runtime(args)
    reference, inferred_task_id = infer_requirement(config, args.requirement)
    task_id = args.task_id or inferred_task_id
    if not task_id:
        raise ValidationError("Requirement must contain task_id or use --task-id")
    workflow = build_external_workflow(config_path, config, store, task_id)
    return workflow.start(
        reference,
        task_id,
        metadata={"config_path": str(config_path)},
    ).to_dict()


def context_command(args: argparse.Namespace) -> dict[str, Any]:
    config_path, config, store = load_runtime(args)
    state = load_existing_state(store, args.task_id)
    return build_external_workflow(
        config_path, config, store, args.task_id
    ).context(state, args.stage)


def submit_command(args: argparse.Namespace) -> dict[str, Any]:
    config_path, config, store = load_runtime(args)
    state = load_existing_state(store, args.task_id)
    artifact = RunStore.read_json(Path(args.artifact).resolve())
    return build_external_workflow(
        config_path, config, store, args.task_id
    ).submit(state, args.stage, artifact).to_dict()


def prepare_command(args: argparse.Namespace) -> dict[str, Any]:
    config_path, config, store = load_runtime(args)
    state = load_existing_state(store, args.task_id)
    workflow = build_external_workflow(config_path, config, store, args.task_id)
    if state.status == "needs_revision":
        feedback: dict[str, Any] = {"error": state.error}
        if "verification" in state.artifacts:
            feedback["verification"] = store.load_artifact(state, "verification")
        build_executor(config_path, config, store).cleanup_for_revision(state)
        state = workflow.begin_revision(state, feedback)
    return workflow.prepare(state).to_dict()


def capture_command(args: argparse.Namespace) -> dict[str, Any]:
    config_path, config, store = load_runtime(args)
    state = load_existing_state(store, args.task_id)
    result = RunStore.read_json(Path(args.result).resolve())
    return build_external_workflow(
        config_path, config, store, args.task_id
    ).capture(state, result).to_dict()


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
        build_agents(config, config_path, store=store, task_id=args.task_id),
        max_retries=int(pipeline.get("max_retries", 3)),
        development_validator=build_development_validator(config, config_path),
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


def logs_command(args: argparse.Namespace) -> list[dict[str, Any]]:
    _, _, store = load_runtime(args)
    if not store.state_path(args.task_id).exists() and not store.read_events(args.task_id):
        raise PipelineError(f"Run '{args.task_id}' does not exist", code="run_not_found")
    displayed = 0
    events: list[dict[str, Any]] = []
    while True:
        current = store.read_events(args.task_id)
        for event in current[displayed:]:
            print(json.dumps(event, ensure_ascii=False))
        events = current
        displayed = len(current)
        if not args.follow:
            return events
        state = store.load_or_create(args.task_id)
        settled = {
            "failed",
            "awaiting_human_review",
            "needs_revision",
            "ready_to_merge",
        }
        if state.status in TERMINAL_STATUSES | settled:
            return events
        time.sleep(0.5)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(normalize_argv(argv))
    try:
        handlers = {
            "run": run_command,
            "start": start_command,
            "context": context_command,
            "submit": submit_command,
            "prepare": prepare_command,
            "capture": capture_command,
            "approve": approve_command,
            "reject": reject_command,
            "status": status_command,
            "logs": logs_command,
            "revise": revise_command,
            "merge": merge_command,
        }
        result = handlers[args.command](args)
        if args.command not in {"status", "logs"}:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except PipelineError as exc:
        error = json.dumps({"error": exc.code, "message": str(exc)}, ensure_ascii=False)
        print(error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
