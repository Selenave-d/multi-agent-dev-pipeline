from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any

from .contracts import RunState, utc_now
from .errors import ValidationError


class RunStore:
    """Persists run state and artifacts using same-directory atomic replacements."""

    MAX_TOOL_OUTPUT_CHARS = 100_000

    def __init__(self, runs_dir: Path) -> None:
        self.runs_dir = runs_dir.resolve()
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def run_dir(self, task_id: str) -> Path:
        safe = "".join(c for c in task_id if c.isalnum() or c in "-_")
        if not safe or safe != task_id:
            raise ValidationError("task_id may contain only letters, numbers, '-' and '_'")
        path = (self.runs_dir / safe).resolve()
        if self.runs_dir not in path.parents:
            raise ValidationError("Invalid task_id path")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_json(self, path: Path, data: dict[str, Any]) -> str:
        payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"Cannot read valid JSON from {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise ValidationError(f"Expected a JSON object in {path}")
        return value

    def state_path(self, task_id: str) -> Path:
        return self.run_dir(task_id) / "run_state.json"

    def load_or_create(self, task_id: str) -> RunState:
        path = self.state_path(task_id)
        return RunState.from_dict(self.read_json(path)) if path.exists() else RunState(task_id)

    def list_states(self) -> list[RunState]:
        states: list[RunState] = []
        for path in sorted(self.runs_dir.glob("*/run_state.json")):
            states.append(RunState.from_dict(self.read_json(path)))
        return sorted(states, key=lambda state: state.updated_at, reverse=True)

    def save_state(self, state: RunState) -> None:
        state.updated_at = utc_now()
        self.write_json(self.state_path(state.task_id), state.to_dict())

    def save_artifact(
        self,
        state: RunState,
        stage: str,
        filename: str,
        data: dict[str, Any],
    ) -> None:
        path = self.run_dir(state.task_id) / filename
        checksum = self.write_json(path, data)
        state.artifacts[stage] = {"file": filename, "sha256": checksum}
        self.save_state(state)

    def load_artifact(self, state: RunState, stage: str) -> dict[str, Any]:
        metadata = state.artifacts.get(stage)
        if not metadata:
            raise ValidationError(f"No artifact recorded for stage '{stage}'")
        path = self.run_dir(state.task_id) / metadata["file"]
        data = self.read_json(path)
        canonical = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        checksum = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if checksum != metadata["sha256"]:
            raise ValidationError(f"Checksum mismatch for stage '{stage}'")
        return data

    def remove_artifact(self, state: RunState, stage: str) -> None:
        metadata = state.artifacts.pop(stage, None)
        if metadata:
            (self.run_dir(state.task_id) / metadata["file"]).unlink(missing_ok=True)
        self.save_state(state)

    @staticmethod
    def _redact(value: Any) -> Any:
        sensitive = ("token", "secret", "password", "api_key", "apikey", "authorization")
        if isinstance(value, dict):
            return {
                key: "***"
                if any(part in key.lower() for part in sensitive)
                else RunStore._redact(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [RunStore._redact(item) for item in value]
        if isinstance(value, str):
            redacted = re.sub(
                r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+",
                r"\1***",
                value,
            )
            redacted = re.sub(
                r"(?i)((?:api[_-]?key|token|secret|password)\s*[:=]\s*)\S+",
                r"\1***",
                redacted,
            )
            for name, secret in os.environ.items():
                if any(part in name.lower() for part in sensitive) and len(secret) >= 4:
                    redacted = redacted.replace(secret, "***")
            return redacted
        return value

    def redact(self, value: Any) -> Any:
        return self._redact(value)

    def append_event(self, task_id: str, event: dict[str, Any]) -> None:
        payload = {"timestamp": utc_now(), **self._redact(event)}
        path = self.run_dir(task_id) / "events.jsonl"
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def read_events(self, task_id: str) -> list[dict[str, Any]]:
        path = self.run_dir(task_id) / "events.jsonl"
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    events.append(value)
        return events

    def save_tool_output(
        self,
        task_id: str,
        stage: str,
        tool: str,
        *,
        stdout: str,
        stderr: str,
    ) -> dict[str, str]:
        output_dir = self.run_dir(task_id) / "tool-output"
        output_dir.mkdir(parents=True, exist_ok=True)
        prefix = f"{stage}-{tool}-{uuid.uuid4().hex[:8]}"
        paths: dict[str, str] = {}
        for stream, content in (("stdout", stdout), ("stderr", stderr)):
            path = output_dir / f"{prefix}.{stream}.log"
            redacted = str(self._redact(content))
            if len(redacted) > self.MAX_TOOL_OUTPUT_CHARS:
                redacted = "[truncated to last 100000 characters]\n" + redacted[
                    -self.MAX_TOOL_OUTPUT_CHARS :
                ]
            path.write_text(redacted, encoding="utf-8", newline="\n")
            paths[stream] = str(path.relative_to(self.run_dir(task_id)))
        return paths
