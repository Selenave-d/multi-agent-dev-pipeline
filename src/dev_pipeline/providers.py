from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from .errors import ValidationError
from .storage import RunStore


class RequirementSource(Protocol):
    def fetch(self, reference: str) -> dict[str, Any]: ...


class ModelClient(Protocol):
    def generate(self, stage: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class FileRequirementSource:
    def fetch(self, reference: str) -> dict[str, Any]:
        path = Path(reference).resolve()
        if not path.is_file():
            raise ValidationError(f"Requirement file does not exist: {path}")
        return RunStore.read_json(path)


class DemoModelClient:
    """Deterministic offline provider used to exercise the complete pipeline safely."""

    def generate(self, stage: str, payload: dict[str, Any]) -> dict[str, Any]:
        requirement = payload["requirement"]
        task_id = requirement["task_id"]
        if stage == "analysis":
            return {
                "task_id": task_id,
                "analysis": {
                    "affected_files": [
                        "src/views/user/UserList.vue",
                        "src/api/user.js",
                    ],
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
