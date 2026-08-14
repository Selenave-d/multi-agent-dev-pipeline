# Development patch 失败诊断

- 症状：Claude 独立运行可修改代码，但 pipeline 中生成的补丁偶发无法通过 apply，重试次数很快耗尽。
- 当前根因边界：development 已由 Git 从临时 worktree 生成 diff，不再是模型手工计算 hunk；若仍失败，应优先检查换行、工作树基线和 apply 策略差异。
- 证据缺口：失败补丁只存在于内存，PatchValidator 拒绝后没有落盘，无法复盘实际内容。
- 修复：在校验前原子保存最新 `development.patch`；校验和批准应用统一使用 `git apply --3way`；默认重试数提高到 3。
- 本机复现：系统 Git 配置为 `core.autocrlf=true`，主目录与新 worktree 的物理换行可能不同；校验和应用增加 `--ignore-space-change`，避免只因 context 空白差异失败。
