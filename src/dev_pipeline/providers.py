from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .development import DevelopmentWorkspace
from .errors import PipelineError, ValidationError
from .formatting import DevelopmentFormatter
from .storage import RunStore


class RequirementSource(Protocol):
    def fetch(self, reference: str) -> dict[str, Any]: ...


class ModelClient(Protocol):
    def generate(self, stage: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class CommandRunner(Protocol):
    def run(
        self,
        command: list[str],
        *,
        cwd: Path,
        input_text: str | None = None,
        timeout: int = 600,
    ) -> str: ...


class SubprocessCommandRunner:
    """Runs an already authenticated coding CLI and returns stdout."""

    def __init__(self, observer: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.observer = observer

    def run(
        self,
        command: list[str],
        *,
        cwd: Path,
        input_text: str | None = None,
        timeout: int = 600,
    ) -> str:
        executable = shutil.which(command[0])
        if not executable:
            raise PipelineError(
                f"Command not found: {command[0]}",
                code="provider_not_installed",
            )
        started = time.monotonic()
        if self.observer:
            self.observer({"event": "tool_started", "tool": command[0], "cwd": str(cwd)})
        try:
            result = subprocess.run(
                [executable, *command[1:]],
                cwd=cwd,
                input=input_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            if self.observer:
                self.observer(
                    {
                        "event": "tool_failed",
                        "tool": command[0],
                        "duration_seconds": round(time.monotonic() - started, 3),
                        "error": f"timed out after {timeout}s",
                    }
                )
            raise PipelineError(
                f"Provider command timed out after {timeout}s: {command[0]}",
                code="provider_timeout",
            ) from exc
        if self.observer:
            self.observer(
                {
                    "event": "tool_finished" if result.returncode == 0 else "tool_failed",
                    "tool": command[0],
                    "exit_code": result.returncode,
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                }
            )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-2000:]
            raise PipelineError(
                f"Provider command failed ({result.returncode}): {detail}",
                code="provider_command_failed",
            )
        return result.stdout


class FileRequirementSource:
    def fetch(self, reference: str) -> dict[str, Any]:
        path = Path(reference).resolve()
        if not path.is_file():
            raise ValidationError(f"Requirement file does not exist: {path}")
        return RunStore.read_json(path)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def http_error_302(
        self,
        request: Any,
        response: Any,
        code: int,
        message: str,
        headers: Any,
    ) -> Any:
        return response

    http_error_301 = http_error_302
    http_error_303 = http_error_302
    http_error_307 = http_error_302
    http_error_308 = http_error_302


@dataclass(frozen=True)
class ZenTaoConfig:
    base_url: str
    code: str
    key: str
    account: str


class ZenTaoRequirementSource:
    """Read-only adapter for ZenTao 21.x story and bug detail endpoints."""

    def __init__(
        self,
        config_path: Path | None = None,
        *,
        expected_product_code: str | None = None,
        timeout: int = 30,
        opener: Any | None = None,
    ) -> None:
        self.config_path = (config_path or Path.home() / ".zentao.json").expanduser()
        self.expected_product_code = expected_product_code
        self.timeout = timeout
        self.opener = opener or urllib.request.build_opener(_NoRedirectHandler())
        self._session_cookie: str | None = None

    @staticmethod
    def parse_reference(reference: str) -> tuple[str, str]:
        match = re.fullmatch(r"(?:(story|bug)[:#-]?)?(\d+)", reference.strip(), re.I)
        if not match:
            raise ValidationError("ZenTao reference must look like story:123, bug:123, or 123")
        return (match.group(1) or "story").lower(), match.group(2)

    @classmethod
    def task_id_for(cls, reference: str) -> str:
        object_type, object_id = cls.parse_reference(reference)
        return f"{object_type.upper()}-{object_id}"

    def fetch(self, reference: str) -> dict[str, Any]:
        object_type, object_id = self.parse_reference(reference)
        config = self._load_config()
        data = self._authenticated_get(
            config,
            f"/{object_type}-view-{object_id}.json",
        )
        item = data.get(object_type, {})
        if not isinstance(item, dict) or not item.get("id"):
            raise PipelineError(
                f"ZenTao {object_type} {object_id} was not found in the response",
                code="zentao_not_found",
            )
        description_fields = ("spec", "verify") if object_type == "story" else ("steps",)
        description = "\n\n".join(
            self._strip_html(str(item.get(field, "")))
            for field in description_fields
            if item.get(field)
        )
        product_code: str | None = None
        if self.expected_product_code:
            product_id = item.get("product")
            product_data = self._authenticated_get(config, f"/product-view-{product_id}.json")
            product = product_data.get("product", {})
            product_code = str(product.get("code") or "") if isinstance(product, dict) else ""
            if product_code != self.expected_product_code:
                raise PipelineError(
                    f"ZenTao requirement {reference} belongs to product code "
                    f"'{product_code}', expected '{self.expected_product_code}'. "
                    "Correct project.zentao_product in config.",
                    code="product_mismatch",
                    retryable=False,
                )
        raw_data = {
            "id": item.get("id"),
            "type": object_type,
            "status": item.get("status"),
            "product": item.get("product"),
            "source_url": f"{config.base_url}/{object_type}-view-{object_id}.html",
        }
        if product_code is not None:
            raw_data["product_code"] = product_code
        return {
            "task_id": f"{object_type.upper()}-{object_id}",
            "title": str(item.get("title", "")),
            "description": description or "No description provided",
            "priority": str(item.get("pri") or item.get("priority") or "medium"),
            "module": str(item.get("module") or "unknown"),
            "raw_data": raw_data,
        }

    def _load_config(self) -> ZenTaoConfig:
        file_config: dict[str, Any] = {}
        if self.config_path.is_file():
            file_config = RunStore.read_json(self.config_path)
        values = {
            "base_url": os.getenv("ZENTAO_BASE_URL") or file_config.get("base_url"),
            "code": os.getenv("ZENTAO_CODE") or file_config.get("code"),
            "key": os.getenv("ZENTAO_KEY") or file_config.get("key"),
            "account": os.getenv("ZENTAO_ACCOUNT") or file_config.get("account"),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ValidationError(
                f"Missing ZenTao configuration: {', '.join(missing)}. "
                "Use ~/.zentao.json or ZENTAO_* environment variables."
            )
        return ZenTaoConfig(
            base_url=str(values["base_url"]).rstrip("/"),
            code=str(values["code"]),
            key=str(values["key"]),
            account=str(values["account"]),
        )

    def _login(self, config: ZenTaoConfig) -> str:
        timestamp = int(time.time())
        token = hashlib.md5(  # noqa: S324 - required by ZenTao's legacy API protocol
            f"{config.code}{config.key}{timestamp}".encode()
        ).hexdigest()
        query = urllib.parse.urlencode(
            {
                "m": "user",
                "f": "apilogin",
                "account": config.account,
                "code": config.code,
                "time": timestamp,
                "token": token,
            }
        )
        response = self._open(f"{config.base_url}/api.php?{query}")
        cookie = response.headers.get("Set-Cookie", "")
        match = re.search(r"zentaosid=([^;]+)", cookie)
        if not match:
            raise PipelineError("ZenTao login did not return zentaosid", code="zentao_auth_failed")
        self._session_cookie = f"zentaosid={match.group(1)}"
        return self._session_cookie

    def _authenticated_get(
        self,
        config: ZenTaoConfig,
        path: str,
        *,
        retried: bool = False,
    ) -> dict[str, Any]:
        cookie = self._session_cookie or self._login(config)
        request = urllib.request.Request(
            f"{config.base_url}{path}",
            headers={"Cookie": cookie, "Accept": "application/json"},
        )
        response = self._open(request)
        status = getattr(response, "status", 200)
        if status in {302, 401, 403} and not retried:
            self._session_cookie = None
            self._login(config)
            return self._authenticated_get(config, path, retried=True)
        payload = self._parse_payload(response.read().decode("utf-8", errors="replace"))
        return payload

    def _open(self, request: str | urllib.request.Request) -> Any:
        try:
            return self.opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            raise PipelineError(
                f"ZenTao HTTP error {exc.code}", code="zentao_http_error"
            ) from exc
        except urllib.error.URLError as exc:
            raise PipelineError(
                f"ZenTao connection failed: {exc.reason}",
                code="zentao_unreachable",
            ) from exc

    @staticmethod
    def _parse_payload(text: str) -> dict[str, Any]:
        try:
            payload = json.loads(text)
            if payload.get("status") == "success" and isinstance(payload.get("data"), str):
                payload = json.loads(payload["data"])
        except (json.JSONDecodeError, AttributeError) as exc:
            raise PipelineError(
                "ZenTao returned invalid JSON",
                code="zentao_invalid_response",
            ) from exc
        if not isinstance(payload, dict):
            raise PipelineError("ZenTao response is not an object", code="zentao_invalid_response")
        return payload

    @staticmethod
    def _strip_html(value: str) -> str:
        return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


class ProjectContext:
    """Builds bounded, text-only context for coding tools."""

    def __init__(
        self,
        root: Path,
        project_config: dict[str, Any] | None = None,
        *,
        max_files: int = 300,
        max_file_bytes: int = 100_000,
    ) -> None:
        self.root = root.resolve()
        self.project_config = project_config or {}
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes

    def tree(self) -> list[str]:
        ignored = {".git", ".venv", "node_modules", "dist", "build", "runs", "__pycache__"}
        files: list[str] = []
        if not self.root.is_dir():
            raise ValidationError(f"Project root does not exist: {self.root}")
        for path in sorted(self.root.rglob("*")):
            if any(part in ignored for part in path.relative_to(self.root).parts):
                continue
            if path.is_file():
                files.append(path.relative_to(self.root).as_posix())
                if len(files) >= self.max_files:
                    break
        return files

    def read_files(self, relative_paths: list[str]) -> dict[str, str]:
        contents: dict[str, str] = {}
        for relative in relative_paths:
            path = (self.root / relative).resolve()
            if self.root not in path.parents or not path.is_file():
                continue
            if path.stat().st_size > self.max_file_bytes:
                contents[relative] = f"[skipped: file exceeds {self.max_file_bytes} bytes]"
                continue
            try:
                contents[relative] = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                contents[relative] = "[skipped: binary or non-UTF-8 file]"
        return contents


ANALYSIS_SCHEMA = {
    "type": "object",
    "required": ["task_id", "analysis"],
    "properties": {
        "task_id": {"type": "string"},
        "analysis": {
            "type": "object",
            "required": [
                "change_status",
                "affected_files",
                "changes",
                "dependencies",
                "estimated_complexity",
            ],
            "properties": {
                "change_status": {
                    "type": "string",
                    "enum": ["changes_required", "already_satisfied"],
                },
                "affected_files": {"type": "array", "items": {"type": "string"}},
                "changes": {"type": "array", "items": {"type": "object"}},
                "dependencies": {"type": "array", "items": {"type": "string"}},
                "estimated_complexity": {"type": "string"},
                "assumptions": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
}

DEVELOPMENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["task_id", "change_status", "changes", "commit_message"],
    "properties": {
        "task_id": {"type": "string"},
        "change_status": {
            "type": "string",
            "enum": ["changes_required", "already_satisfied"],
        },
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["file", "diff"],
                "properties": {"file": {"type": "string"}, "diff": {"type": "string"}},
            },
        },
        "commit_message": {"type": "string"},
        "verification": {"type": "array", "items": {"type": "string"}},
    },
}

DEVELOPMENT_EDIT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["task_id", "change_status", "commit_message"],
    "properties": {
        "task_id": {"type": "string"},
        "change_status": {
            "type": "string",
            "enum": ["changes_required", "already_satisfied"],
        },
        "commit_message": {"type": "string"},
    },
}

REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["task_id", "review_result", "issues", "summary"],
    "properties": {
        "task_id": {"type": "string"},
        "review_result": {
            "type": "string",
            "enum": ["pass", "pass_with_suggestions", "changes_requested"],
        },
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["severity", "message"],
                "properties": {
                    "severity": {"type": "string"},
                    "message": {"type": "string"},
                },
            },
        },
        "summary": {"type": "string"},
    },
}


class CodingClientBase:
    def __init__(
        self,
        project: ProjectContext,
        *,
        runner: CommandRunner | None = None,
        command: str,
        model: str | None = None,
        timeout: int = 600,
    ) -> None:
        self.project = project
        self.runner = runner or SubprocessCommandRunner()
        self.command = command
        self.model = model
        self.timeout = timeout

    @staticmethod
    def parse_json_text(text: str) -> dict[str, Any]:
        candidate = text.strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", candidate, re.S)
        if fenced:
            candidate = fenced.group(1)
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            start, end = candidate.find("{"), candidate.rfind("}")
            if start < 0 or end <= start:
                raise PipelineError(
                    "Provider output contained no JSON object",
                    code="invalid_model_output",
                ) from None
            try:
                value = json.loads(candidate[start : end + 1])
            except json.JSONDecodeError as exc:
                raise PipelineError(
                    f"Provider output was not valid JSON: {exc}", code="invalid_model_output"
                ) from exc
        if not isinstance(value, dict):
            raise PipelineError("Provider JSON must be an object", code="invalid_model_output")
        return value


class KimiCodeClient(CodingClientBase):
    """Kimi Code analysis adapter using non-interactive JSONL output."""

    def __init__(self, project: ProjectContext, **kwargs: Any) -> None:
        super().__init__(project, command=kwargs.pop("command", "kimi"), **kwargs)

    def generate(self, stage: str, payload: dict[str, Any]) -> dict[str, Any]:
        if stage != "analysis":
            raise ValidationError("KimiCodeClient only supports the analysis stage")
        prompt = self._analysis_prompt(payload)
        command = [self.command, "-p", prompt, "--output-format", "stream-json"]
        if self.model:
            command[1:1] = ["--model", self.model]
        output = self.runner.run(command, cwd=self.project.root, timeout=self.timeout)
        return self._parse_stream(output)

    def _analysis_prompt(self, payload: dict[str, Any]) -> str:
        context = {
            "requirement": payload["requirement"],
            "project": self.project.project_config,
            "files": self.project.tree(),
        }
        return (
            "你是需求分析 Agent。只分析和规划，严禁写代码、修改文件或执行命令。"
            "必须先用只读能力检查当前实现，特别是已有的校验、去重、兼容和边界处理。"
            "如果现有代码已满足需求，设置 change_status=already_satisfied，并给出空 changes；"
            "只有确认存在缺口时才设置 change_status=changes_required。"
            "基于检查结果识别受影响文件、逐文件修改计划、依赖、复杂度和假设。"
            "只输出一个符合下方 JSON Schema 的 JSON 对象，不要 Markdown。\n"
            f"Schema: {json.dumps(ANALYSIS_SCHEMA, ensure_ascii=False)}\n"
            f"Input: {json.dumps(context, ensure_ascii=False)}"
        )

    def _parse_stream(self, output: str) -> dict[str, Any]:
        assistant_contents: list[str] = []
        for line in output.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("role") == "assistant" and isinstance(event.get("content"), str):
                assistant_contents.append(event["content"])
        if not assistant_contents:
            raise PipelineError("Kimi returned no assistant message", code="invalid_model_output")
        return self.parse_json_text(assistant_contents[-1])


class ClaudeCodeClient(CodingClientBase):
    """Lets Claude edit an isolated worktree and uses Git to produce the patch."""

    def __init__(
        self,
        project: ProjectContext,
        *,
        worktree_path: Path | None = None,
        formatter: DevelopmentFormatter | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        **kwargs: Any,
    ) -> None:
        self.worktree_path = (worktree_path or project.root / ".pipeline-development").resolve()
        self.formatter = formatter or DevelopmentFormatter(project.root)
        self.event_callback = event_callback
        self.workspace = DevelopmentWorkspace(
            project.root,
            self.worktree_path,
            formatter=self.formatter,
            event_callback=event_callback,
        )
        super().__init__(project, command=kwargs.pop("command", "claude"), **kwargs)

    def generate(self, stage: str, payload: dict[str, Any]) -> dict[str, Any]:
        if stage != "development":
            raise ValidationError("ClaudeCodeClient only supports the development stage")
        context = {
            "requirement": payload["requirement"],
            "analysis": payload["analysis"],
            "project": self.project.project_config,
            "revision_feedback": payload.get("revision_feedback"),
            "development_validation_feedback": payload.get(
                "development_validation_feedback"
            ),
        }
        prompt = (
            "你是代码开发 Agent。当前目录是从目标仓库 HEAD 创建的隔离 worktree。"
            "必须先读取 analysis 中的 inspect、change_status、changes 和现状结论。"
            "如果分析表明需求已被当前代码满足，必须收手：返回 change_status="
            "already_satisfied，不得修改文件。只有确实需要修改时，直接编辑 worktree 中的文件，"
            "返回 change_status=changes_required。不要生成或返回 unified diff；diff 由 Git 生成。"
            "不得执行 shell 命令。只返回符合指定 JSON Schema 的结构化结果。\n"
            f"Input: {json.dumps(context, ensure_ascii=False)}"
        )
        command = [
            self.command,
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(DEVELOPMENT_EDIT_SCHEMA),
            "--tools",
            "Read,Edit,Write,Glob,Grep",
            "--allowedTools",
            "Read,Edit,Write,Glob,Grep",
            "--permission-mode",
            "acceptEdits",
            "--no-session-persistence",
        ]
        if self.model:
            command.extend(["--model", self.model])
        try:
            self.workspace.prepare()
            output = self.runner.run(
                command, cwd=self.worktree_path, input_text=prompt, timeout=self.timeout
            )
            envelope = self.parse_json_text(output)
            structured = envelope.get("structured_output")
            result = structured if isinstance(structured, dict) else envelope.get("result")
            response = (
                result
                if isinstance(result, dict)
                else self.parse_json_text(result)
                if isinstance(result, str)
                else envelope
            )
            return self.workspace.capture(response)
        finally:
            self.workspace.cleanup()


class ReviewClient(CodingClientBase):
    """Independent review adapter selectable between Kimi, Claude, and Codex."""

    def __init__(self, project: ProjectContext, *, tool: str = "codex", **kwargs: Any) -> None:
        self.tool = tool
        default_command = {"kimi": "kimi", "claude": "claude", "codex": "codex"}.get(tool)
        if not default_command:
            raise ValidationError(f"Unsupported review tool: {tool}")
        super().__init__(project, command=kwargs.pop("command", default_command), **kwargs)

    def generate(self, stage: str, payload: dict[str, Any]) -> dict[str, Any]:
        if stage != "review":
            raise ValidationError("ReviewClient only supports the review stage")
        context = {
            "requirement": payload["requirement"],
            "analysis": payload["analysis"],
            "development": payload["development"],
            "project": self.project.project_config,
        }
        prompt = (
            "你是独立代码审查 Agent。检查逻辑、边界情况、安全性、项目规范和测试充分性。"
            "不要修改代码。只输出符合 JSON Schema 的审查对象。\n"
            f"Schema: {json.dumps(REVIEW_SCHEMA, ensure_ascii=False)}\n"
            f"Input: {json.dumps(context, ensure_ascii=False)}"
        )
        if self.tool == "kimi":
            command = [self.command, "-p", prompt, "--output-format", "stream-json"]
            if self.model:
                command[1:1] = ["--model", self.model]
            output = self.runner.run(command, cwd=self.project.root, timeout=self.timeout)
            return KimiCodeClient._parse_stream(self, output)
        if self.tool == "claude":
            command = [
                self.command,
                "-p",
                "--output-format",
                "json",
                "--json-schema",
                json.dumps(REVIEW_SCHEMA),
                "--tools",
                "",
                "--no-session-persistence",
            ]
            if self.model:
                command.extend(["--model", self.model])
            envelope = self.parse_json_text(
                self.runner.run(
                    command,
                    cwd=self.project.root,
                    input_text=prompt,
                    timeout=self.timeout,
                )
            )
            structured = envelope.get("structured_output")
            if isinstance(structured, dict):
                return structured
            result = envelope.get("result")
            return self.parse_json_text(result) if isinstance(result, str) else envelope
        return self._run_codex(prompt)

    def _run_codex(self, prompt: str) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="pipeline-codex-") as temp_dir:
            schema_path = Path(temp_dir) / "review.schema.json"
            output_path = Path(temp_dir) / "review.json"
            schema_path.write_text(json.dumps(REVIEW_SCHEMA), encoding="utf-8")
            command = [
                self.command,
                "exec",
                "-C",
                str(self.project.root),
                "--sandbox",
                "read-only",
                "-c",
                "approval_policy=never",
                "--ephemeral",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "-",
            ]
            if self.model:
                command[2:2] = ["--model", self.model]
            stdout = self.runner.run(
                command,
                cwd=self.project.root,
                input_text=prompt,
                timeout=self.timeout,
            )
            text = output_path.read_text(encoding="utf-8") if output_path.is_file() else stdout
            return self.parse_json_text(text)


class DemoModelClient:
    """Deterministic offline provider used to exercise the complete pipeline safely."""

    def generate(self, stage: str, payload: dict[str, Any]) -> dict[str, Any]:
        requirement = payload["requirement"]
        task_id = requirement["task_id"]
        if stage == "analysis":
            return {
                "task_id": task_id,
                "analysis": {
                    "change_status": "changes_required",
                    "affected_files": ["src/views/user/UserList.vue", "src/api/user.js"],
                    "changes": [
                        {
                            "file": "src/views/user/UserList.vue",
                            "action": "modify",
                            "description": "添加搜索输入与搜索事件，并处理空关键词",
                        },
                        {
                            "file": "src/api/user.js",
                            "action": "modify",
                            "description": "为用户列表请求增加可选 keyword 参数",
                        },
                    ],
                    "dependencies": [],
                    "estimated_complexity": "medium",
                    "assumptions": ["后端列表接口已支持 keyword 查询参数"],
                },
            }
        if stage == "development":
            return {
                "task_id": task_id,
                "change_status": "changes_required",
                "changes": [
                    {
                        "file": "src/views/user/UserList.vue",
                        "diff": (
                            "--- a/src/views/user/UserList.vue\n"
                            "+++ b/src/views/user/UserList.vue\n"
                            "@@ -1,3 +1,4 @@\n"
                            "+<!-- 示例变更：接入搜索栏；"
                            "需在目标仓库中由开发 Agent 生成真实补丁 -->\n"
                        ),
                    }
                ],
                "commit_message": "feat: 用户列表添加搜索功能",
                "verification": ["npm run lint", "npm test"],
            }
        if stage == "review":
            return {
                "task_id": task_id,
                "review_result": "pass_with_suggestions",
                "issues": [
                    {
                        "severity": "warning",
                        "file": "src/views/user/UserList.vue",
                        "line": 1,
                        "message": "接入真实项目时应增加输入防抖和请求竞态处理",
                    }
                ],
                "summary": "离线演示变更的数据契约有效；应用到真实项目之前仍需生成并测试真实补丁。",
            }
        raise ValidationError(f"Demo provider does not support stage: {stage}")
