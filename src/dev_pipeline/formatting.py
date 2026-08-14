from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import PipelineError


@dataclass(frozen=True)
class FormatResult:
    tool: str
    files: list[str]
    stdout: str = ""
    stderr: str = ""


class DevelopmentFormatter:
    """Normalizes changed text files and runs a project-local formatter when available."""

    def __init__(
        self,
        project_root: Path,
        *,
        command: str | None = None,
        auto_detect: bool = True,
        timeout: int = 300,
    ) -> None:
        self.project_root = project_root.resolve()
        self.command = command
        self.auto_detect = auto_detect
        self.timeout = timeout

    def format(self, worktree: Path, files: list[str]) -> FormatResult:
        worktree = worktree.resolve()
        safe_files = self._safe_existing_files(worktree, files)
        normalized = [path for path in safe_files if self._ensure_final_newline(worktree / path)]
        command, tool = self._resolve_command(safe_files)
        if not command or not safe_files:
            return FormatResult(tool="builtin-newline", files=normalized)
        try:
            result = subprocess.run(
                command,
                cwd=worktree,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise PipelineError(
                f"Development formatter timed out after {self.timeout}s: {tool}",
                code="development_format_failed",
            ) from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-4000:]
            raise PipelineError(
                f"Development formatter failed ({tool}): {detail}",
                code="development_format_failed",
            )
        return FormatResult(
            tool=tool,
            files=safe_files,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    def _resolve_command(self, files: list[str]) -> tuple[list[str] | None, str]:
        if self.command:
            parts = shlex.split(self.command, posix=os.name != "nt")
            if "{files}" in parts:
                index = parts.index("{files}")
                parts[index : index + 1] = files
            else:
                parts.extend(files)
            return parts, parts[0] if parts else "configured"
        if not self.auto_detect:
            return None, "builtin-newline"

        prettier = self._local_prettier()
        if prettier and self._uses_prettier():
            return [str(prettier), "--write", "--ignore-unknown", *files], "prettier"

        pyproject = self.project_root / "pyproject.toml"
        pyproject_text = pyproject.read_text(encoding="utf-8") if pyproject.is_file() else ""
        ruff = shutil.which("ruff")
        if ruff and "[tool.ruff" in pyproject_text:
            python_files = [path for path in files if path.endswith(".py")]
            return ([ruff, "format", *python_files], "ruff") if python_files else (None, "ruff")
        black = shutil.which("black")
        if black and "[tool.black" in pyproject_text:
            python_files = [path for path in files if path.endswith(".py")]
            return ([black, *python_files], "black") if python_files else (None, "black")
        gofmt = shutil.which("gofmt")
        if gofmt and (self.project_root / "go.mod").is_file():
            go_files = [path for path in files if path.endswith(".go")]
            return ([gofmt, "-w", *go_files], "gofmt") if go_files else (None, "gofmt")
        return None, "builtin-newline"

    def _local_prettier(self) -> Path | None:
        name = "prettier.cmd" if os.name == "nt" else "prettier"
        candidate = self.project_root / "node_modules" / ".bin" / name
        return candidate if candidate.is_file() else None

    def _uses_prettier(self) -> bool:
        config_names = (
            ".prettierrc",
            ".prettierrc.json",
            ".prettierrc.json5",
            ".prettierrc.js",
            ".prettierrc.cjs",
            ".prettierrc.mjs",
            ".prettierrc.toml",
            ".prettierrc.yaml",
            ".prettierrc.yml",
            "prettier.config.js",
            "prettier.config.cjs",
            "prettier.config.mjs",
            "prettier.config.ts",
        )
        if any((self.project_root / name).is_file() for name in config_names):
            return True
        package_path = self.project_root / "package.json"
        if not package_path.is_file():
            return False
        try:
            package = json.loads(package_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return False
        if not isinstance(package, dict):
            return False
        dependencies = package.get("dependencies")
        dev_dependencies = package.get("devDependencies")
        return (
            "prettier" in package
            or isinstance(dependencies, dict)
            and "prettier" in dependencies
            or isinstance(dev_dependencies, dict)
            and "prettier" in dev_dependencies
        )

    @staticmethod
    def _safe_existing_files(worktree: Path, files: list[str]) -> list[str]:
        safe: list[str] = []
        for name in files:
            relative = Path(name)
            candidate = (worktree / relative).resolve()
            if relative.is_absolute() or worktree not in candidate.parents:
                raise PipelineError(
                    f"Formatter path escapes development worktree: {name}",
                    code="unsafe_format_path",
                    retryable=False,
                )
            if candidate.is_file():
                safe.append(candidate.relative_to(worktree).as_posix())
        return safe

    @staticmethod
    def _ensure_final_newline(path: Path) -> bool:
        content = path.read_bytes()
        if not content or b"\x00" in content:
            return False
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            return False
        normalized = text.rstrip("\r\n") + "\n"
        if normalized == text:
            return False
        path.write_text(normalized, encoding="utf-8", newline="\n")
        return True
