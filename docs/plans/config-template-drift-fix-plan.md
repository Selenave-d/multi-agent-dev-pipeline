# 配置模板漂移修复计划

1. 将本地 `config.json` 对齐当前 `config.demo.json` 的分阶段 provider 和 worktree 配置。
2. 从 `config.example.json` 删除无代码引用的 `stop_after`。
3. 增加配置模板测试，确保 `stop_after` 不再出现，并校验 `worktree_dir` 与 `runs_dir` 一致。
4. 运行测试、Ruff、配置解析和差异检查。
