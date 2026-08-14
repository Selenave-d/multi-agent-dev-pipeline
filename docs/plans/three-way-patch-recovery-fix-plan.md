# Development patch 恢复修复计划

1. Claude worktree 生成 diff 后原子写入 `<run>/development.patch`，校验失败仍保留。
2. PatchValidator 在一次性 detached worktree 中使用 `git apply --3way -`，不修改主工作区。
3. approve 在隔离 worktree 使用 `git apply --3way`。
4. Orchestrator、CLI 默认值以及示例/本地运行配置的 `max_retries` 统一为 3。
5. 补充 patch 留存、3-way 参数和默认重试测试，运行全量验证及交叉 Review。
