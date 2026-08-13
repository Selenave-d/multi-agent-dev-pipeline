from __future__ import annotations

import json
from pathlib import Path

import pytest

from dev_pipeline.errors import ValidationError
from dev_pipeline.providers import (
    ClaudeCodeClient,
    KimiCodeClient,
    ProjectContext,
    ReviewClient,
    ZenTaoRequirementSource,
)


class FakeRunner:
    def __init__(self, output: str, *, output_file: dict | None = None) -> None:
        self.output = output
        self.output_file = output_file
        self.calls: list[dict] = []

    def run(self, command, *, cwd, input_text=None, timeout=600):
        self.calls.append(
            {"command": command, "cwd": cwd, "input_text": input_text, "timeout": timeout}
        )
        if self.output_file and "--output-last-message" in command:
            index = command.index("--output-last-message") + 1
            Path(command[index]).write_text(json.dumps(self.output_file), encoding="utf-8")
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
            "changes": [{"file": "src/app.py", "diff": "--- a\n+++ b"}],
            "commit_message": "feat: search",
        },
    }


def project(tmp_path: Path) -> ProjectContext:
    source = tmp_path / "src"
    source.mkdir()
    (source / "app.py").write_text("print('hello')\n", encoding="utf-8")
    return ProjectContext(tmp_path, {"name": "test"})


def test_kimi_analysis_builds_read_only_prompt_and_parses_jsonl(tmp_path: Path) -> None:
    result = {
        "task_id": "STORY-1",
        "analysis": {
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


def test_claude_development_uses_schema_and_source_files(tmp_path: Path) -> None:
    result = {
        "task_id": "STORY-1",
        "changes": [{"file": "src/app.py", "diff": "--- a\n+++ b"}],
        "commit_message": "feat: search",
    }
    runner = FakeRunner(json.dumps({"structured_output": result}))
    client = ClaudeCodeClient(project(tmp_path), runner=runner, model="sonnet")

    assert client.generate("development", analysis_payload()) == result
    command = runner.calls[0]["command"]
    assert command[0:2] == ["claude", "-p"]
    assert "--json-schema" in command
    assert command[command.index("--tools") + 1] == ""
    assert "--no-session-persistence" in command
    prompt = command[2]
    assert "不要修改工作区" in prompt
    assert "print('hello')" in prompt


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
    assert "不要修改代码" in runner.calls[0]["command"][2]


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
    assert command[command.index("--ask-for-approval") + 1] == "never"
    assert command[-1] == "-"


class FakeResponse:
    def __init__(self, body: dict | None = None, cookie: str | None = None) -> None:
        self.body = json.dumps(body or {}).encode()
        self.headers = {"Set-Cookie": cookie or ""}

    def read(self) -> bytes:
        return self.body


class FakeOpener:
    def __init__(self) -> None:
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        url = request if isinstance(request, str) else request.full_url
        if "apilogin" in url:
            return FakeResponse(cookie="zentaosid=test-session; path=/")
        data = {
            "story": {
                "id": "42",
                "title": "用户搜索",
                "pri": "1",
                "module": "7",
                "status": "active",
                "product": "2",
                "spec": "<p>添加关键词搜索</p>",
                "verify": "<p>空关键词返回全部</p>",
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


def test_zentao_reference_validation() -> None:
    assert ZenTaoRequirementSource.task_id_for("123") == "STORY-123"
    assert ZenTaoRequirementSource.task_id_for("bug:9") == "BUG-9"
    with pytest.raises(ValidationError):
        ZenTaoRequirementSource.task_id_for("../../secret")
