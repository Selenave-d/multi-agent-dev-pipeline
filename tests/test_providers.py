from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from dev_pipeline.errors import PipelineError, ValidationError
from dev_pipeline.formatting import FormatResult
from dev_pipeline.patches import PatchValidator
from dev_pipeline.providers import (
    DEVELOPMENT_SCHEMA,
    ClaudeCodeClient,
    KimiCodeClient,
    ProjectContext,
    ReviewClient,
    ZenTaoRequirementSource,
)


def test_development_schema_is_strict() -> None:
    assert DEVELOPMENT_SCHEMA["additionalProperties"] is False
    assert DEVELOPMENT_SCHEMA["properties"]["changes"]["items"]["additionalProperties"] is False


class FakeRunner:
    def __init__(
        self,
        output: str,
        *,
        output_file: dict | None = None,
        edit: tuple[str, str] | None = None,
    ) -> None:
        self.output = output
        self.output_file = output_file
        self.edit = edit
        self.calls: list[dict] = []

    def run(self, command, *, cwd, input_text=None, timeout=600):
        self.calls.append(
            {"command": command, "cwd": cwd, "input_text": input_text, "timeout": timeout}
        )
        if self.output_file and "--output-last-message" in command:
            index = command.index("--output-last-message") + 1
            Path(command[index]).write_text(json.dumps(self.output_file), encoding="utf-8")
        if self.edit:
            (cwd / self.edit[0]).write_text(self.edit[1], encoding="utf-8")
        return self.output


def requirement_payload() -> dict:
    return {
        "requirement": {
            "task_id": "STORY-1",
            "title": "搜索",
            "description": "增加搜索",
            "priority": "1",
            "module": "用户",
        }
    }


def analysis_payload() -> dict:
    return {
        **requirement_payload(),
        "analysis": {
            "task_id": "STORY-1",
            "analysis": {
                "change_status": "changes_required",
                "affected_files": ["src/app.py"],
                "changes": [],
                "dependencies": [],
                "estimated_complexity": "low",
            },
        },
    }


def development_payload() -> dict:
    return {
        **analysis_payload(),
        "development": {
            "task_id": "STORY-1",
            "change_status": "changes_required",
            "changes": [{"file": "src/app.py", "diff": "--- a\n+++ b"}],
            "commit_message": "feat: search",
        },
    }


def project(tmp_path: Path) -> ProjectContext:
    source = tmp_path / "src"
    source.mkdir()
    (source / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (tmp_path / "other.txt").write_text("unchanged\n", encoding="utf-8")
    return ProjectContext(tmp_path, {"name": "test"})


def git_project(tmp_path: Path) -> ProjectContext:
    context = project(tmp_path)
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "core.autocrlf", "false"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "add", "--all"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "-m",
            "init",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    return context


def test_kimi_analysis_builds_read_only_prompt_and_parses_jsonl(tmp_path: Path) -> None:
    result = {
        "task_id": "STORY-1",
        "analysis": {
            "change_status": "changes_required",
            "affected_files": ["src/app.py"],
            "changes": [],
            "dependencies": [],
            "estimated_complexity": "low",
        },
    }
    runner = FakeRunner(
        '\n'.join(
            [
                json.dumps({"role": "assistant", "content": "正在分析"}),
                json.dumps({"role": "tool", "content": "ignored"}),
                json.dumps({"role": "assistant", "content": json.dumps(result)}),
            ]
        )
    )
    client = KimiCodeClient(project(tmp_path), runner=runner, model="kimi-code")

    assert client.generate("analysis", requirement_payload()) == result
    command = runner.calls[0]["command"]
    assert command[:3] == ["kimi", "--model", "kimi-code"]
    assert "--output-format" in command
    prompt = command[command.index("-p") + 1]
    assert "只分析和规划" in prompt
    assert "严禁写代码、修改文件或执行命令" in prompt
    assert "src/app.py" in prompt


def test_claude_development_edits_worktree_and_git_generates_diff(tmp_path: Path) -> None:
    response = {
        "task_id": "STORY-1",
        "change_status": "changes_required",
        "commit_message": "feat: search",
    }
    runner = FakeRunner(
        json.dumps({"structured_output": response}),
        edit=("src/app.py", "print('changed')"),
    )
    worktree = tmp_path.parent / "development-worktree"
    client = ClaudeCodeClient(
        git_project(tmp_path), runner=runner, model="sonnet", worktree_path=worktree
    )

    result = client.generate("development", analysis_payload())

    assert result["change_status"] == "changes_required"
    assert result["changes"][0]["file"] == "src/app.py"
    assert "-print('hello')" in result["changes"][0]["diff"]
    assert "+print('changed')" in result["changes"][0]["diff"]
    assert "No newline at end of file" not in result["changes"][0]["diff"]
    PatchValidator(tmp_path).validate(result)
    saved_patch = worktree.parent / "development.patch"
    assert saved_patch.read_text(encoding="utf-8") == result["changes"][0]["diff"]
    raw_patch = (worktree.parent / "development.raw.patch").read_text(encoding="utf-8")
    assert "No newline at end of file" in raw_patch
    assert not worktree.exists()
    assert (tmp_path / "src/app.py").read_text(encoding="utf-8") == "print('hello')\n"
    call = runner.calls[0]
    command = call["command"]
    assert command[0:2] == ["claude", "-p"]
    assert "--json-schema" in command
    assert command[command.index("--tools") + 1] == "Read,Edit,Write,Glob,Grep"
    assert command[command.index("--allowedTools") + 1] == "Read,Edit,Write,Glob,Grep"
    assert command[command.index("--permission-mode") + 1] == "acceptEdits"
    assert "--no-session-persistence" in command
    prompt = call["input_text"]
    assert "直接编辑 worktree" in prompt
    assert "diff 由 Git 生成" in prompt


def test_claude_development_accepts_noop_and_cleans_worktree(tmp_path: Path) -> None:
    response = {
        "task_id": "STORY-1",
        "change_status": "already_satisfied",
        "commit_message": "chore: no changes needed",
    }
    worktree = tmp_path.parent / "development-worktree"
    client = ClaudeCodeClient(
        git_project(tmp_path),
        runner=FakeRunner(json.dumps({"structured_output": response})),
        worktree_path=worktree,
    )

    result = client.generate("development", analysis_payload())

    assert result["change_status"] == "already_satisfied"
    assert result["changes"] == []
    assert not worktree.exists()


def test_claude_development_captures_new_files_in_git_diff(tmp_path: Path) -> None:
    response = {
        "task_id": "STORY-1",
        "change_status": "changes_required",
        "commit_message": "feat: add helper",
    }
    client = ClaudeCodeClient(
        git_project(tmp_path),
        runner=FakeRunner(
            json.dumps({"structured_output": response}),
            edit=("new.py", "value = 1\n"),
        ),
        worktree_path=tmp_path.parent / "development-worktree",
    )

    result = client.generate("development", analysis_payload())

    assert result["changes"][0]["file"] == "new.py"
    assert "new file mode" in result["changes"][0]["diff"]


def test_claude_development_rejects_dirty_project_without_retry(tmp_path: Path) -> None:
    context = git_project(tmp_path)
    (tmp_path / "src" / "app.py").write_text("dirty\n", encoding="utf-8")
    runner = FakeRunner(
        json.dumps(
            {
                "structured_output": {
                    "task_id": "STORY-1",
                    "change_status": "changes_required",
                    "commit_message": "feat: change",
                }
            }
        )
    )
    client = ClaudeCodeClient(
        context,
        runner=runner,
        worktree_path=tmp_path.parent / "development-worktree",
    )

    with pytest.raises(PipelineError) as error:
        client.generate("development", analysis_payload())

    assert error.value.code == "project_dirty"
    assert error.value.retryable is False
    assert runner.calls == []


class ExpandingFormatter:
    def format(self, worktree: Path, files: list[str]) -> FormatResult:
        (worktree / "formatter-created.txt").write_text(
            "formatter created this\n", encoding="utf-8"
        )
        return FormatResult(tool="test", files=files)


def test_claude_rejects_formatter_changes_outside_original_set(tmp_path: Path) -> None:
    response = {
        "task_id": "STORY-1",
        "change_status": "changes_required",
        "commit_message": "feat: search",
    }
    worktree = tmp_path.parent / "development-worktree"
    client = ClaudeCodeClient(
        git_project(tmp_path),
        runner=FakeRunner(
            json.dumps({"structured_output": response}),
            edit=("src/app.py", "print('changed')"),
        ),
        formatter=ExpandingFormatter(),
        worktree_path=worktree,
    )

    with pytest.raises(PipelineError) as error:
        client.generate("development", analysis_payload())

    assert error.value.code == "unexpected_format_changes"
    assert error.value.retryable is False
    assert (worktree.parent / "development.raw.patch").is_file()
    assert not worktree.exists()


@pytest.mark.parametrize("tool", ["kimi", "claude"])
def test_review_client_parses_kimi_and_claude(tool: str, tmp_path: Path) -> None:
    result = {
        "task_id": "STORY-1",
        "review_result": "pass",
        "issues": [],
        "summary": "ok",
    }
    if tool == "kimi":
        output = json.dumps({"role": "assistant", "content": json.dumps(result)})
    else:
        output = json.dumps({"structured_output": result})
    runner = FakeRunner(output)
    client = ReviewClient(project(tmp_path), tool=tool, runner=runner)

    assert client.generate("review", development_payload()) == result
    call = runner.calls[0]
    if tool == "kimi":
        prompt = call["command"][call["command"].index("-p") + 1]
    else:
        prompt = call["input_text"]
    assert "不要修改代码" in prompt


def test_codex_review_is_read_only_and_reads_last_message(tmp_path: Path) -> None:
    result = {
        "task_id": "STORY-1",
        "review_result": "pass",
        "issues": [],
        "summary": "ok",
    }
    runner = FakeRunner("event output", output_file=result)
    client = ReviewClient(project(tmp_path), tool="codex", runner=runner)

    assert client.generate("review", development_payload()) == result
    command = runner.calls[0]["command"]
    assert command[:2] == ["codex", "exec"]
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("-c") + 1] == "approval_policy=never"
    assert command[-1] == "-"


class FakeResponse:
    def __init__(self, body: dict | None = None, cookie: str | None = None) -> None:
        self.body = json.dumps(body or {}).encode()
        self.headers = {"Set-Cookie": cookie or ""}

    def read(self) -> bytes:
        return self.body


class FakeOpener:
    def __init__(self, product_code: str = "DTS") -> None:
        self.product_code = product_code
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        url = request if isinstance(request, str) else request.full_url
        if "apilogin" in url:
            return FakeResponse(cookie="zentaosid=test-session; path=/")
        if "/product-view-2.json" in url:
            data = {"product": {"id": "2", "code": self.product_code}}
            return FakeResponse({"status": "success", "data": json.dumps(data)})
        object_type = "bug" if "/bug-view-" in url else "story"
        object_id = "6043" if object_type == "bug" else "42"
        data = {
            object_type: {
                "id": object_id,
                "title": "用户搜索",
                "pri": "1",
                "module": "7",
                "status": "active",
                "product": "2",
                "spec": "<p>添加关键词搜索</p>" if object_type == "story" else None,
                "verify": "<p>空关键词返回全部</p>" if object_type == "story" else None,
                "steps": "<p>搜索结果错误</p>" if object_type == "bug" else None,
            }
        }
        wrapped = {"status": "success", "data": json.dumps(data)}
        return FakeResponse(wrapped)


def test_zentao_fetch_logs_in_and_normalizes_story(tmp_path: Path) -> None:
    config = tmp_path / "zentao.json"
    config.write_text(
        json.dumps(
            {
                "base_url": "http://zentao.test/zentao",
                "code": "code",
                "key": "key",
                "account": "user",
            }
        ),
        encoding="utf-8",
    )
    opener = FakeOpener()
    source = ZenTaoRequirementSource(config, opener=opener)

    result = source.fetch("story:42")

    assert result["task_id"] == "STORY-42"
    assert result["title"] == "用户搜索"
    assert result["description"] == "添加关键词搜索\n\n空关键词返回全部"
    assert result["priority"] == "1"
    assert result["module"] == "7"
    login_url = opener.requests[0]
    assert "m=user&f=apilogin" in login_url
    detail_request = opener.requests[1]
    assert detail_request.full_url.endswith("/story-view-42.json")
    assert detail_request.headers["Cookie"] == "zentaosid=test-session"
    assert len(opener.requests) == 2


def write_zentao_config(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "base_url": "http://zentao.test/zentao",
                "code": "code",
                "key": "key",
                "account": "user",
            }
        ),
        encoding="utf-8",
    )


def test_zentao_product_code_match_is_traceable(tmp_path: Path) -> None:
    config = tmp_path / "zentao.json"
    write_zentao_config(config)
    opener = FakeOpener(product_code="DTS")
    source = ZenTaoRequirementSource(
        config,
        expected_product_code="DTS",
        opener=opener,
    )

    result = source.fetch("story:42")

    assert result["raw_data"]["product_code"] == "DTS"
    product_request = opener.requests[2]
    assert product_request.full_url.endswith("/product-view-2.json")
    assert product_request.headers["Cookie"] == "zentaosid=test-session"


def test_zentao_product_code_mismatch_stops_requirement(tmp_path: Path) -> None:
    config = tmp_path / "zentao.json"
    write_zentao_config(config)
    source = ZenTaoRequirementSource(
        config,
        expected_product_code="DTS",
        opener=FakeOpener(product_code="BV-GIS-AR"),
    )

    with pytest.raises(PipelineError) as error:
        source.fetch("bug:6043")

    assert error.value.code == "product_mismatch"
    assert error.value.retryable is False
    assert "DTS" in str(error.value)
    assert "BV-GIS-AR" in str(error.value)
    assert "bug:6043" in str(error.value)
    assert "project.zentao_product" in str(error.value)


def test_zentao_without_expected_product_skips_product_lookup(tmp_path: Path) -> None:
    config = tmp_path / "zentao.json"
    write_zentao_config(config)
    opener = FakeOpener(product_code="BV-GIS-AR")
    source = ZenTaoRequirementSource(config, opener=opener)

    result = source.fetch("story:42")

    assert "product_code" not in result["raw_data"]
    assert len(opener.requests) == 2


def test_zentao_reference_validation() -> None:
    assert ZenTaoRequirementSource.task_id_for("123") == "STORY-123"
    assert ZenTaoRequirementSource.task_id_for("bug:9") == "BUG-9"
    with pytest.raises(ValidationError):
        ZenTaoRequirementSource.task_id_for("../../secret")
