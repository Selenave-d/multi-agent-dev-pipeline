# ADR-0001: Use a host Agent Skill with the Pipeline kernel

## Status

Accepted

## Context

Provider 驱动的 CLI 能独立运行，但在 Codex Desktop 中使用时不够直观：使用者已经位于目标项目，却还要切换到 Pipeline 仓库并配置多个模型 CLI。宿主已经具备任务交互和子 Agent 能力，而补丁、验证和合并等步骤仍需要可复现的确定性边界。

## Decision

采用混合架构：Codex Skill 作为默认交互入口和宿主 Agent 调度器；Pipeline 作为执行内核。外部阶段接口接收宿主生成的结构化 analysis/review 产物。开发 Agent 只编辑 Pipeline 创建的隔离 worktree，Pipeline 负责格式化、Git diff、补丁校验、批准后验证、合并和工作日志。现有 Provider 模式继续作为后备入口。

## Consequences

### Positive

- 使用者可以在目标项目中直接发起任务。
- Agent 编排利用宿主能力，不需要为每个阶段重复维护模型 CLI 基座。
- 确定性安全门禁仍只有一份实现。

### Negative

- Skill 与 Pipeline CLI 之间增加了一组阶段协议。
- 宿主模式依赖 Codex 的 Agent 能力；纯终端环境仍需 Provider。

### Neutral

- analysis、development、review 仍使用相同 JSON 契约和运行产物。

## Alternatives Considered

**只保留 Provider CLI**：部署独立，但桌面交互割裂，模型适配成本持续增长。

**完全由 Skill 实现全部流程**：入口简单，但会复制状态、补丁、验证和合并逻辑，安全边界难以统一。

**把测试交给测试 Agent 自由执行**：灵活，但结果不稳定且难复现；因此测试 Agent 只解释证据，命令执行仍归 Pipeline。

## References

- `docs/plans/2026-08-14-host-agent-skill-plan.md`
- `docs/brainstorms/host-agent-skill-requirements.md`
