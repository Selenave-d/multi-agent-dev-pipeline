---
name: dev-pipeline
description: Run a safe multi-Agent software change from the current Git project using the external multi-agent-dev-pipeline kernel. Use when the user asks to analyze, implement, review, test, approve, revise, or merge a code requirement through the Pipeline, especially from Codex Desktop with host subagents.
---

# Dev Pipeline

Treat the current Git repository as the target project. Use host subagents for judgment and the Pipeline CLI for state, worktrees, patches, verification, approval, merge, and work logs.

## Resolve the project

1. Run `git rev-parse --show-toplevel` from the current directory.
2. Require `.dev-pipeline.json` at that Git root unless the user provides `--config`.
3. Require `project.root` to resolve to that same Git root. Stop on a mismatch.
4. Invoke `dev-pipeline` from `PATH`. If unavailable, use the Pipeline repository virtualenv supplied by the user or ask them to install the package.
5. Require the target worktree to be clean before `prepare`, `approve`, or `merge`.

Do not switch to the Pipeline repository as the working project.

## Run the host-Agent workflow

1. Start with `dev-pipeline start --requirement <reference> [--task-id <id>]`.
2. Read analysis input with `dev-pipeline context --task-id <id> --stage analysis`.
3. Delegate analysis to a read-only subagent. Require a JSON object matching the analysis artifact contract. The subagent must inspect current code, identify `already_satisfied` when appropriate, and must not edit files.
4. Save only the JSON result to a temporary file and run `dev-pipeline submit --task-id <id> --stage analysis --artifact <file>`.
5. Run `dev-pipeline prepare --task-id <id>`. Read the returned `metadata.development_worktree_path`.
6. Read the complete development input with `dev-pipeline context --task-id <id> --stage development`, then delegate implementation to a development subagent working only in that worktree. It must follow the saved analysis and edit files directly; it must not create a diff or commit.
7. Write a small result JSON containing `task_id`, `change_status`, and `commit_message`. Run `dev-pipeline capture --task-id <id> --result <file>`. The Pipeline formats changed files, generates the Git diff, validates it, and removes the development worktree after success.
8. If the status is `no_changes_needed`, report the analysis and stop.
9. Read review input with `dev-pipeline context --task-id <id> --stage review`.
10. Delegate review to a fresh, independent read-only subagent. Do not reuse the development subagent. Require a JSON object matching the review artifact contract.
11. Submit with `dev-pipeline submit --task-id <id> --stage review --artifact <file>`.
12. Present the review summary and changed files to the user. Never approve or merge without the user's explicit instruction.

## Complete or revise

- On approval, run `dev-pipeline approve --task-id <id>` interactively. The Pipeline applies the patch and runs configured lint, test, build, and browser checks.
- If verification returns `needs_revision`, run `dev-pipeline prepare --task-id <id>` again. It cleans the failed approval worktree, resets development/review artifacts, and exposes verification evidence through development context. Do not bypass a failing gate.
- When status is `ready_to_merge`, run `dev-pipeline merge --task-id <id>` only after explicit user authorization.
- Use `dev-pipeline logs --task-id <id> --follow` for long-running progress and `dev-pipeline status` for summaries.

## Boundaries

- Keep Kimi, Claude Code, and Codex CLI providers as fallback mode; do not invoke them when host subagents are available unless the user chooses that mode.
- Let deterministic commands decide test success. A test subagent may interpret logs, but must not replace lint/test/build/browser execution.
- Do not edit the main target worktree during development.
- Do not hand-write or repair unified diff text. Fix files in the development worktree and recapture.
- Do not commit, push, approve, reject, or merge unless the user explicitly asks for that action.
