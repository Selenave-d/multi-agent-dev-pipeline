# 配置模板漂移诊断

- 症状：本地 `config.json` 仍使用旧版扁平 provider，并指向不存在的 `examples/sample_project`；示例配置包含未被代码读取的 `stop_after`。
- 根因：provider 与 worktree 配置升级时，本地忽略文件没有迁移；已提交模板缺少“只包含有效字段”的回归测试。
- 影响：Demo 分析仍可能运行，但真实 worktree 阶段会因项目根不存在而失败；`stop_after` 会误导使用者认为流程支持阶段截断。
