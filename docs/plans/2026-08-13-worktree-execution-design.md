# Worktree 执行闭环设计

## 目标

把 Pipeline 从“只生成和审查 diff”升级为“人工批准后，在隔离 Git worktree 中应用、验证并等待合并”。主工作区在批准前后都不能被 Agent 直接修改，最终合并仍需独立人工命令。

## 状态流

```text
awaiting_human_review
  ├─ reject  → rejected
  └─ approve → approved → apply → verify
                         ├─ failed → needs_revision → revise → awaiting_human_review
                         └─ passed → ready_to_merge → merge → merged
```

审批和验证分别生成 `05_decision.json` 与 `06_verification.json`。`run_state.json` 记录目标仓库、基准分支、基准提交、任务分支和 worktree 路径，使中断后的命令不依赖会话记忆。

## Git 边界

批准时要求目标仓库干净且处于普通分支。Pipeline 基于当前 HEAD 创建 `pipeline/<task-id>` 分支，并在 `runs/<task-id>/worktree` 建立 worktree。补丁在一次性 worktree 中用 `git apply --3way` 校验，批准后再以相同策略正式应用。

验证成功后不立即提交。`merge` 命令再次要求主工作区干净、仍在原分支、HEAD 仍等于批准时的基准提交；之后在任务 worktree 提交变更，并用 `git merge --no-ff` 合并。发生冲突会中止 merge，不自动解决、不强制覆盖，也不推送远程。

## 验证与修订

lint、test、build 从配置读取，顺序执行并在首次失败时停止。每步保存命令、退出码、耗时以及截断后的 stdout/stderr。失败状态为 `needs_revision`；`revise` 先验证状态，再删除旧 worktree 和任务分支，把错误与验证证据作为 `revision_feedback` 传给开发 Agent，随后重新生成 development 和 review 产物。

## 验证策略

自动化测试使用临时 Git 仓库覆盖完整成功合并、命令失败、拒绝、主分支漂移、错误状态清理保护和旧状态兼容。Demo 流程继续验证原有四阶段行为，Ruff 保证静态质量。
