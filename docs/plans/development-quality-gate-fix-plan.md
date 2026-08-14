# Development 质量门修复计划

1. 扩展 analysis/development JSON Schema，声明 `change_status`。
2. development prompt 必须读取 analysis 的 inspect/现状结论；已满足时返回 `already_satisfied` 和空 changes。
3. 调整契约校验，允许合法 no-op，拒绝状态与 changes 不一致。
4. 在保存 development 产物前，在一次性 worktree 中运行 `git apply --3way`。
5. 校验失败时将错误反馈给下一次 development 生成；所有尝试失败才落入 failed 状态。
6. 覆盖 no-op、坏 diff 重试、坏 diff 不落盘及正常 diff，并运行全量验证和交叉 Review。
7. development 为 `already_satisfied` 时立即收尾，run/revise 均不执行 review，并清理 revise 的旧 review 产物。
8. PatchValidator 在检查 diff 前拒绝脏工作区；将 development schema 顶层和 changes item 设为 strict。
