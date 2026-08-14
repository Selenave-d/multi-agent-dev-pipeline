# Host Agent Skill 实施计划

1. 抽取 `DevelopmentWorkspace`，供 Claude Provider 和宿主 Agent 流程共同使用。
2. 新增 `ExternalWorkflow`，实现阶段顺序、上下文输出、产物提交和状态更新。
3. CLI 新增 `start`、`context`、`submit`、`prepare`、`capture` 命令。
4. README 和配置示例补充 `.dev-pipeline.json` 与 Skill 模式说明。
5. 在仓库中创建可安装 Skill，验证元数据与工作流说明。
6. 新增单元/CLI 测试并执行 pytest、ruff 和交叉审查。
