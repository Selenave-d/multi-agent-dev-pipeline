from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from dev_pipeline.worklog import WorkLogWriter

NOW = datetime(2026, 8, 14, 9, 7)


def memory_path(home: Path, project_root: Path) -> Path:
    encoded = re.sub(r"[^A-Za-z0-9_-]", "-", str(project_root.resolve()))
    return home / ".claude" / "projects" / encoded / "memory" / "obsidian-log-path.md"


def write_memory(path: Path, log_directory: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"- 日志目录: `{log_directory}`\n", encoding="utf-8")


def test_configured_path_overrides_project_and_global_memory(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    configured = tmp_path / "configured"
    write_memory(memory_path(tmp_path, project), tmp_path / "project-log")
    write_memory(
        tmp_path / ".claude" / "memory" / "obsidian-log-path.md",
        tmp_path / "global-log",
    )
    writer = WorkLogWriter(project, configured_path=configured, home=tmp_path)

    assert writer.resolve_log_directory() == (configured.resolve(), "configured")


def test_project_memory_overrides_global_memory(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    project_log = tmp_path / "project-log"
    write_memory(memory_path(tmp_path, project), project_log)
    write_memory(
        tmp_path / ".claude" / "memory" / "obsidian-log-path.md",
        tmp_path / "global-log",
    )

    assert WorkLogWriter(project, home=tmp_path).resolve_log_directory() == (
        project_log.resolve(),
        "project_memory",
    )


def test_global_memory_is_used_as_fallback(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    global_log = tmp_path / "global-log"
    write_memory(tmp_path / ".claude" / "memory" / "obsidian-log-path.md", global_log)

    assert WorkLogWriter(project, home=tmp_path).resolve_log_directory() == (
        global_log.resolve(),
        "global_memory",
    )


def test_worklog_creates_daily_file_and_bug_section(tmp_path: Path) -> None:
    project = tmp_path / "sample-project"
    project.mkdir()
    log_directory = tmp_path / "logs"
    writer = WorkLogWriter(
        project,
        configured_path=log_directory,
        home=tmp_path,
        clock=lambda: NOW,
    )

    result = writer.write(
        {
            "title": "修正 Claude AI 登录页异常",
            "raw_data": {"type": "bug"},
        }
    )

    content = (log_directory / "2026-08-14.md").read_text(encoding="utf-8")
    assert content == (
        "# 2026-08-14 工作日志\n\n"
        "## [sample-project]\n"
        "- [09:07] 修复: 修正 登录页异常\n"
    )
    assert result.status == "written"
    assert "Claude" not in content
    assert "AI" not in content


def test_worklog_appends_inside_existing_project_section(tmp_path: Path) -> None:
    project = tmp_path / "sample-project"
    project.mkdir()
    log_directory = tmp_path / "logs"
    log_directory.mkdir()
    daily = log_directory / "2026-08-14.md"
    daily.write_text(
        "# 2026-08-14 工作日志\n\n"
        "## [sample-project]\n"
        "- [08:00] 优化: 旧条目\n\n"
        "## [another-project]\n"
        "- [08:30] 实现: 其他内容\n",
        encoding="utf-8",
    )
    writer = WorkLogWriter(project, configured_path=log_directory, clock=lambda: NOW)

    writer.write({"title": "新增查询入口", "raw_data": {"type": "story"}})

    content = daily.read_text(encoding="utf-8")
    new_entry = "- [09:07] 实现: 新增查询入口"
    assert content.index(new_entry) < content.index("## [another-project]")
    assert content.count("## [sample-project]") == 1


def test_worklog_uses_optimize_for_other_requirement_types(tmp_path: Path) -> None:
    project = tmp_path / "sample-project"
    project.mkdir()
    writer = WorkLogWriter(
        project,
        configured_path=tmp_path / "logs",
        clock=lambda: NOW,
    )

    result = writer.write({"title": "整理筛选体验", "raw_data": {"type": "task"}})

    assert result.entry == "- [09:07] 优化: 整理筛选体验"


def test_worklog_does_not_remove_ai_inside_an_ordinary_word(tmp_path: Path) -> None:
    project = tmp_path / "sample-project"
    project.mkdir()
    writer = WorkLogWriter(
        project,
        configured_path=tmp_path / "logs",
        clock=lambda: NOW,
    )

    result = writer.write({"title": "Improve detail page"})

    assert result.entry == "- [09:07] 优化: Improve detail page"


def test_worklog_skips_when_no_path_can_be_resolved(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    result = WorkLogWriter(project, home=tmp_path).write({"title": "任意需求"})

    assert result.status == "skipped"
    assert result.reason == "path_not_found"
