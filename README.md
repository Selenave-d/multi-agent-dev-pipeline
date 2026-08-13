# Multi-Agent Dev Pipeline

一个面向小型开发团队的多 Agent 协作开发流程 MVP。当前版本跑通：

```text
需求文件 → 需求标准化 → 需求分析 → 代码变更建议 → 独立 Review → 等待人工确认
```

它默认完全离线运行，不需要 API Key，也不会修改业务代码、执行 Git 提交或自动合并。离线 `demo` 适配器用于验证流程、数据契约和故障恢复；接入真实模型和禅道时只需实现已有接口。

## 已实现

- 四个可替换 Agent，共用 `execute(input_data) -> dict` 接口
- JSON 阶段产物与统一的 `task_id`、`created_at`、`errors` 字段
- 原子文件写入及 SHA-256 完整性校验
- 明确的运行状态、阶段尝试次数、错误信息和 `--resume` 断点续跑
- 重复任务保护：已有运行必须显式使用 `--resume`
- 路径约束：任务 ID 不能逃逸 `runs` 目录
- 确定性的离线端到端演示和自动化测试

## 环境要求

- Python 3.10+

## 快速开始

PowerShell：

```powershell
cd D:\work-git\multi-agent-dev-pipeline
Copy-Item config.example.json config.json
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\dev-pipeline.exe --config config.json --requirement examples\requirement.json
```

运行结束后，控制台状态应为 `awaiting_human_review`，产物位于：

```text
runs/REQ-20260813-001/
├── 01_requirement.json
├── 02_analysis.json
├── 03_code_changes.json
├── 04_review.json
└── run_state.json
```

若进程在某阶段失败，修复配置或服务后继续：

```powershell
.\.venv\Scripts\dev-pipeline.exe --config config.json --requirement examples\requirement.json --resume
```

## 架构

```text
CLI
 └─ Orchestrator
     ├─ RequirementAgent ─ RequirementSource (当前：JSON 文件；后续：禅道)
     ├─ AnalysisAgent    ┐
     ├─ DevelopmentAgent├─ ModelClient (当前：demo；后续：模型 SDK/CLI)
     └─ ReviewAgent      ┘
          │
          └─ RunStore（原子 JSON、校验和、断点状态）
```

核心扩展接口：

```python
class RequirementSource(Protocol):
    def fetch(self, reference: str) -> dict: ...

class ModelClient(Protocol):
    def generate(self, stage: str, payload: dict) -> dict: ...
```

真实模型的输出必须仍满足各阶段契约。Review Agent 只接收需求、分析和变更上下文，不与开发 Agent 共享隐式对话，从而保留独立审查边界。

## 测试

```powershell
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\python -m ruff check .
```

测试覆盖完整流程、重复运行保护、失败恢复、产物篡改检测和路径逃逸防护。

## 当前安全边界

- 不读取或修改配置项目根目录中的业务代码。
- 不执行模型返回的命令或 diff。
- 不自动提交、推送或合并 Git 分支。
- 不把 API 密钥写入 JSON 产物。
- 禅道写回、人工批准和 Git Agent 留到下一阶段实现。

## 下一阶段

1. 增加真实 `ModelClient` 适配器，并用 JSON Schema/结构化输出约束模型响应。
2. 增加只读禅道 `RequirementSource`，认证信息仅来自环境变量或密钥管理器。
3. 增加人工决策产物 `05_decision.json`，批准后才能进入 Git Agent。
4. 在隔离 worktree 应用补丁并运行项目级 lint/test；仍不直接合并主分支。
