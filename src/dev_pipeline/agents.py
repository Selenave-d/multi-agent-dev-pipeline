from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from .contracts import utc_now
from .providers import ModelClient, RequirementSource


class BaseAgent(ABC):
    name: str

    @abstractmethod
    def execute(self, input_data: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @staticmethod
    def envelope(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "created_at": utc_now(), "errors": data.get("errors", [])}


class RequirementAgent(BaseAgent):
    name = "requirement"

    def __init__(self, source: RequirementSource) -> None:
        self.source = source

    def execute(self, input_data: dict[str, Any]) -> dict[str, Any]:
        raw = self.source.fetch(input_data["reference"])
        normalized = {
            "task_id": raw["task_id"],
            "title": raw["title"],
            "description": raw["description"],
            "priority": raw.get("priority", "medium"),
            "module": raw.get("module", "unknown"),
            "raw_data": raw,
        }
        return self.envelope(normalized)


class ModelAgent(BaseAgent):
    def __init__(self, name: str, model: ModelClient) -> None:
        self.name = name
        self.model = model

    def execute(self, input_data: dict[str, Any]) -> dict[str, Any]:
        return self.envelope(self.model.generate(self.name, input_data))
