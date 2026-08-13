# Development 质量门诊断

- 症状：analysis 已判断代码具备所需实现，development 仍生成修改；损坏 diff 直到 approve 才被发现。
- 根因：analysis/development 契约没有明确的“无需修改”状态；development 保存前只校验 JSON 字段，没有验证 diff 是否适用于目标仓库；重试不携带补丁校验错误。
- 影响：无意义修改进入 Review，格式损坏的补丁占用完整流程后才失败。
- 修复边界：增加精确的 `change_status`；仅对 `changes_required` 执行只读 `git apply --check`，失败走现有开发重试并反馈错误。
- 补充症状：development 返回 `already_satisfied` 后 review 仍执行，空 changes 被判为 `changes_requested`；脏工作区又会让保存前校验与 approve 的干净 worktree 基线不一致。
- 补充根因：编排器只在所有阶段完成后判断 no-op；PatchValidator 未校验仓库状态；development schema 未完全满足 strict structured output。
