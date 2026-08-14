# Multi-Agent Dev Pipeline

一个面向小型开发团队的多 Agent 协作开发流程 MVP。当前版本跑通：

```text
需求文件 → 需求标准化 → 需求分析 → 隔离 worktree 开发 → Git 生成补丁 → 独立 Review → 等待人工确认
```

默认交互方式是在目标项目中使用 Codex Skill，由宿主子 Agent 分别完成分析、开发和独立 Review；Pipeline 负责状态、worktree、补丁与验证。它也保留离线 `demo` 和 Kimi/Claude Code/Codex CLI Provider，供纯终端或无宿主 Agent 的环境使用。无论使用哪种方式，都不会未经人工批准应用补丁或合并代码。

## 已实现

- 四个可替换 Agent，共用 `execute(input_data) -> dict` 接口
- JSON 阶段产物与统一的 `task_id`、`created_at`、`errors` 字段
- 原子文件写入及 SHA-256 完整性校验
- 明确的运行状态、阶段尝试次数、错误信息和 `--resume` 断点续跑
- 重复任务保护：已有运行必须显式使用 `--resume`
- 路径约束：任务 ID 不能逃逸 `runs` 目录
- 确定性的离线端到端演示和自动化测试
- 禅道只读适配器，以及 Kimi Code、Claude Code、Codex CLI 适配器
- 每阶段独立 provider 配置，避免开发与 Review 共用隐式会话
- 每个任务持久化 `events.jsonl` 和脱敏后的工具 stdout/stderr
- `logs --follow` 实时查看阶段、重试、校验和命令执行进度
- 可选的外置 Playwright 页面验收，不向业务项目写入 E2E 测试文件
- merge 成功后尽力同步 Obsidian 工作日志，日志失败不影响合并结果
- 确定性配置/环境错误立即失败，瞬时错误和补丁生成错误继续按配置重试

## 环境要求

- Python 3.10+

## 快速开始

### 在目标项目中使用（推荐）

先安装本项目，再把 `skills/dev-pipeline` 安装到 Codex 的全局 Skills 目录。目标项目根目录放置一个已纳入版本控制的 `.dev-pipeline.json`：

```powershell
cd D:\work-git\multi-agent-dev-pipeline
.\.venv\Scripts\python -m pip install -e ".[dev]"

cd D:\work-git\your-project
Copy-Item D:\work-git\multi-agent-dev-pipeline\.dev-pipeline.example.json .dev-pipeline.json
```

将 `project.root` 保持为 `.`，按项目修改 lint/test/build 命令。示例配置把 `runs_dir` 和 `worktree_dir` 放到用户目录，避免运行产物弄脏目标仓库；若省略这两个字段，CLI 仍使用配置文件旁的 `runs/`。

随后在 Codex Desktop 打开目标项目，直接说：

```text
使用 $dev-pipeline 实现这个需求：<需求文件或禅道引用>
```

Skill 调用的阶段协议如下：

```text
start → analysis 子 Agent → submit
      → prepare worktree → development 子 Agent → capture Git patch
      → independent review 子 Agent → submit
      → 人工 approve → lint/test/build/browser → 人工 merge
```

宿主模式的项目配置只需要 `providers.requirement`；analysis/development/review Provider 不配置也可以。现有 Kimi、Claude Code、Codex Provider 仅作为后备模式保留。

阶段命令也可以手工执行：

```powershell
dev-pipeline start --requirement requirements\TASK-1.json
dev-pipeline context --task-id TASK-1 --stage analysis
dev-pipeline submit --task-id TASK-1 --stage analysis --artifact analysis.json
dev-pipeline prepare --task-id TASK-1
dev-pipeline context --task-id TASK-1 --stage development
# Agent 只编辑 prepare 返回的 development_worktree_path
dev-pipeline capture --task-id TASK-1 --result development-result.json
dev-pipeline context --task-id TASK-1 --stage review
dev-pipeline submit --task-id TASK-1 --stage review --artifact review.json
```

### 独立 CLI / Demo 模式

PowerShell：

```powershell
cd D:\work-git\multi-agent-dev-pipeline
Copy-Item config.demo.json config.json
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
├── events.jsonl
├── tool-output/
└── run_state.json
```

需要启用页面点击验收时，额外安装 Pipeline 自己的浏览器依赖：

```powershell
.\.venv\Scripts\python -m pip install -e ".[dev,browser]"
.\.venv\Scripts\python -m playwright install chromium
```

人工确认并执行：

```powershell
dev-pipeline status --config config.json
dev-pipeline logs --config config.json --task-id REQ-20260813-001 --follow
dev-pipeline approve --config config.json --task-id REQ-20260813-001
# approve 会展示 Review 摘要并等待 y/N；批准后创建隔离 worktree、应用补丁、运行命令和浏览器验证
dev-pipeline merge --config config.json --task-id REQ-20260813-001
```

打回或验证失败后的修订：

```powershell
dev-pipeline reject --config config.json --task-id REQ-20260813-001
dev-pipeline revise --config config.json --task-id REQ-20260813-001
```

状态流：

```text
awaiting_human_review
  ├─ reject  → rejected
  └─ approve → approved → apply/verify
                         ├─ failed → needs_revision → revise → awaiting_human_review
                         └─ passed → ready_to_merge → merge → merged
```

若进程在某阶段失败，修复配置或服务后继续：

```powershell
.\.venv\Scripts\dev-pipeline.exe --config config.json --requirement examples\requirement.json --resume
```

## 架构

```text
Codex Skill（默认）               Provider CLI（后备）
  ├─ analysis subagent             ├─ Kimi analysis
  ├─ development subagent          ├─ Claude development
  └─ independent review subagent   └─ Codex/Kimi/Claude review
                 │                         │
                 └──────────┬──────────────┘
                            ▼
                 Pipeline execution kernel
                 contracts / RunStore / worktree
                 Git patch / verify / approve / merge / worklog
```

核心扩展接口：

```python
class RequirementSource(Protocol):
    def fetch(self, reference: str) -> dict: ...

class ModelClient(Protocol):
    def generate(self, stage: str, payload: dict) -> dict: ...
```

真实模型的输出必须仍满足各阶段契约。Review Agent 只接收需求、分析和变更上下文，不与开发 Agent 共享隐式对话，从而保留独立审查边界。

真实工具配置、凭据和故障排查见 [docs/providers.md](docs/providers.md)，各 CLI 命令、参数和返回格式见 [docs/tool-invocation.md](docs/tool-invocation.md)。

## 测试

```powershell
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\python -m ruff check .
```

测试覆盖完整流程、重复运行保护、失败恢复、产物篡改检测和路径逃逸防护。

## 当前安全边界

- 分析阶段只读取受限目录树；开发阶段在从 HEAD 创建的临时 detached worktree 中运行。
- Claude development 只开放 Read/Edit/Write/Glob/Grep，不开放 shell；diff 由 Git 生成。
- 最新 development 候选会保存在 `runs/<task_id>/development.patch`，校验与批准应用均使用 `git apply --3way`。
- Claude 原始修改保存在 `development.raw.patch`；Pipeline 只对 changed files 自动补齐末尾换行，并在可用时调用项目本地 formatter 后生成最终 `development.patch`。
- 不执行模型返回的命令；临时 development worktree 在成功或失败后均清理。
- 只有人工 `approve` 后才在隔离 worktree 应用 diff 和运行配置命令。
- 页面点击验收由 Pipeline 自带 Playwright 在批准 worktree 中运行；截图和服务日志仅写入对应 run 目录。
- 只有人工 `merge` 后才提交任务分支并 `--no-ff` 合并；不自动推送远程。
- 不把 API 密钥写入 JSON 产物。
- 禅道写回和远程推送仍不在自动执行范围内。

## 下一阶段

1. 根据真实项目补充更严格的 JSON Schema 和文件访问白名单。
2. 增加 apply 失败的结构化反馈和自动补丁修复策略。
3. 增加合并后的远程推送/PR 人工门禁。
