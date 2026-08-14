from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from .errors import PipelineError


@dataclass(frozen=True)
class WorkLogResult:
    status: str
    path: str | None = None
    entry: str | None = None
    reason: str | None = None


class WorkLogSink(Protocol):
    def write(self, requirement: dict[str, Any]) -> WorkLogResult: ...


class WorkLogWriter:
    """Writes a deterministic, non-technical merge summary to an Obsidian daily log."""

    MEMORY_FILE = "obsidian-log-path.md"
    PATH_PATTERN = re.compile(r"日志目录[^\r\n`]*`([^`\r\n]+)`")
    FORBIDDEN_PATTERN = re.compile(
        r"Claude|Kimi|Codex|OpenAI|Anthropic|Gemini|DeepSeek|Qwen|"
        r"GPT(?:-[\w.]+)?|(?<![A-Za-z])AI(?![A-Za-z])|人工智能",
        re.IGNORECASE,
    )

    def __init__(
        self,
        project_root: Path,
        *,
        configured_path: Path | None = None,
        enabled: bool = True,
        home: Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.configured_path = configured_path.expanduser().resolve() if configured_path else None
        self.enabled = enabled
        self.home = (home or Path.home()).expanduser().resolve()
        self.clock = clock or datetime.now

    def write(self, requirement: dict[str, Any]) -> WorkLogResult:
        log_directory, source = self.resolve_log_directory()
        if log_directory is None:
            return WorkLogResult(status="skipped", reason=source)
        now = self.clock()
        path = log_directory / f"{now:%Y-%m-%d}.md"
        section = f"## [{self.project_root.name}]"
        entry = f"- [{now:%H:%M}] {self._summary(requirement)}"
        original = path.read_text(encoding="utf-8") if path.is_file() else ""
        updated = self._insert_entry(original, now, section, entry)
        self._atomic_write(path, updated)
        confirmed = path.read_text(encoding="utf-8")
        if section not in confirmed or entry not in confirmed:
            raise PipelineError(
                f"Work log verification failed after writing {path}",
                code="work_log_write_failed",
                retryable=False,
            )
        return WorkLogResult(status="written", path=str(path), entry=entry, reason=source)

    def resolve_log_directory(self) -> tuple[Path | None, str]:
        if not self.enabled:
            return None, "disabled"
        if self.configured_path is not None:
            return self.configured_path, "configured"
        encoded = re.sub(r"[^A-Za-z0-9_-]", "-", str(self.project_root))
        candidates = (
            (
                self.home
                / ".claude"
                / "projects"
                / encoded
                / "memory"
                / self.MEMORY_FILE,
                "project_memory",
            ),
            (self.home / ".claude" / "memory" / self.MEMORY_FILE, "global_memory"),
        )
        for memory_path, source in candidates:
            path = self._path_from_memory(memory_path)
            if path is not None:
                return path, source
        return None, "path_not_found"

    def _path_from_memory(self, memory_path: Path) -> Path | None:
        if not memory_path.is_file():
            return None
        match = self.PATH_PATTERN.search(memory_path.read_text(encoding="utf-8"))
        if not match:
            return None
        configured = Path(match.group(1).strip()).expanduser()
        if configured.is_absolute():
            return configured.resolve()
        return (memory_path.parent / configured).resolve()

    @classmethod
    def _summary(cls, requirement: dict[str, Any]) -> str:
        raw_data = requirement.get("raw_data")
        requirement_type = raw_data.get("type") if isinstance(raw_data, dict) else None
        verb = {"bug": "修复", "story": "实现"}.get(str(requirement_type).lower(), "优化")
        title = re.sub(r"<[^>]+>", "", str(requirement.get("title", "")))
        title = title.replace("`", "")
        title = cls.FORBIDDEN_PATTERN.sub("", title)
        title = re.sub(r"\s+", " ", title).strip(" :-：—_/，,。")
        if not title:
            title = "完成需求"
        if len(title) > 80:
            title = title[:79].rstrip() + "…"
        return f"{verb}: {title}"

    @staticmethod
    def _insert_entry(original: str, now: datetime, section: str, entry: str) -> str:
        header = f"# {now:%Y-%m-%d} 工作日志"
        if not original.strip():
            return f"{header}\n\n{section}\n{entry}\n"
        lines = original.replace("\r\n", "\n").replace("\r", "\n").splitlines()
        try:
            section_index = lines.index(section)
        except ValueError:
            while lines and not lines[-1].strip():
                lines.pop()
            lines.extend(["", section, entry])
            return "\n".join(lines) + "\n"
        next_section = next(
            (
                index
                for index in range(section_index + 1, len(lines))
                if lines[index].startswith("## ")
            ),
            len(lines),
        )
        insertion = next_section
        while insertion > section_index + 1 and not lines[insertion - 1].strip():
            insertion -= 1
        lines.insert(insertion, entry)
        return "\n".join(lines) + "\n"

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
