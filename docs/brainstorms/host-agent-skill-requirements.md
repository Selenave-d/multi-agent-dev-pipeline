# Host Agent Skill 需求

## 目标

让使用者在目标 Git 项目的 Codex 桌面任务中直接启动开发流程。Codex Skill 负责交互和 Agent 调度，Pipeline 保留阶段契约、产物、worktree、补丁、验证、批准、合并与日志等确定性能力。

## 范围

- 保留 Kimi、Claude Code、Codex CLI Provider，作为无宿主 Agent 环境的后备模式。
- 新增外部阶段接口，允许宿主 Agent 提交 analysis/review 产物。
- development 由宿主开发 Agent 编辑 Pipeline 创建的隔离 worktree，补丁仍由 Git 生成。
- Skill 从当前 Git 根目录查找 `.dev-pipeline.json`，不要求使用者先进入 Pipeline 仓库。
- approve、merge 必须继续由现有 Pipeline 命令执行，不由 Skill 或子 Agent 直接代替。

## 验收标准

- 外部流程可完成 `start/context/submit/prepare/capture` 并进入 `awaiting_human_review`。
- 非法阶段顺序、错误 task_id 和无效产物被拒绝。
- worktree 修改经格式化、Git diff 和 PatchValidator 后才保存 development 产物。
- 原有 provider 驱动模式和全量测试保持可用。
- Skill 可通过 Codex 的 Skill 校验器，并有清晰的当前项目使用指令。
