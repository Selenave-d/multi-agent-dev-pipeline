from __future__ import annotations

import os
from pathlib import Path

import pytest

from dev_pipeline.errors import PipelineError
from dev_pipeline.formatting import DevelopmentFormatter


def test_builtin_formatter_adds_single_final_newline(tmp_path: Path) -> None:
    source = tmp_path / "src" / "app.js"
    source.parent.mkdir()
    source.write_text("const value = 1;\n\n", encoding="utf-8")
    formatter = DevelopmentFormatter(tmp_path, auto_detect=False)

    result = formatter.format(tmp_path, ["src/app.js"])

    assert source.read_bytes() == b"const value = 1;\n"
    assert result.tool == "builtin-newline"
    assert result.files == ["src/app.js"]


def test_builtin_formatter_skips_binary_and_deleted_files(tmp_path: Path) -> None:
    binary = tmp_path / "image.bin"
    binary.write_bytes(b"\x00raw")
    formatter = DevelopmentFormatter(tmp_path, auto_detect=False)

    result = formatter.format(tmp_path, ["image.bin", "deleted.js"])

    assert binary.read_bytes() == b"\x00raw"
    assert result.files == []


def test_formatter_rejects_path_outside_worktree(tmp_path: Path) -> None:
    formatter = DevelopmentFormatter(tmp_path, auto_detect=False)

    with pytest.raises(PipelineError) as error:
        formatter.format(tmp_path, ["../outside.js"])

    assert error.value.code == "unsafe_format_path"


def test_configured_formatter_expands_files_as_separate_arguments(tmp_path: Path) -> None:
    formatter = DevelopmentFormatter(
        tmp_path,
        command="formatter --write {files}",
        auto_detect=False,
    )

    command, tool = formatter._resolve_command(["src/file with spaces.js"])

    assert command == ["formatter", "--write", "src/file with spaces.js"]
    assert tool == "formatter"


def test_formatter_detects_project_local_prettier(tmp_path: Path) -> None:
    executable_name = "prettier.cmd" if os.name == "nt" else "prettier"
    executable = tmp_path / "node_modules" / ".bin" / executable_name
    executable.parent.mkdir(parents=True)
    executable.touch()
    (tmp_path / ".prettierrc").write_text("{}\n", encoding="utf-8")
    formatter = DevelopmentFormatter(tmp_path)

    command, tool = formatter._resolve_command(["src/app.js"])

    assert command == [
        str(executable),
        "--write",
        "--ignore-unknown",
        "src/app.js",
    ]
    assert tool == "prettier"
