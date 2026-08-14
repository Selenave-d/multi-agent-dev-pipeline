# 外置浏览器验收需求

## 目标

- 在批准补丁后的 worktree 验证阶段增加真实页面访问、点击和断言。
- 浏览器测试实现、依赖和运行产物全部属于 Pipeline，不向目标业务仓库写入测试文件。
- 未配置浏览器验收时保持现有 lint/test/build 行为不变。
- 失败时保存可复现的步骤、页面错误、服务日志和截图，并进入 `needs_revision`。

## 方案比较

1. 复用业务项目 Cypress/Playwright：生态成熟，但需要每个项目添加测试文件，不符合外置要求。
2. Pipeline 内置 Playwright：统一、可复现、可截图，业务项目只需在 Pipeline 配置中声明启动命令和场景。采用此方案。
3. AI 视觉自由探索：无需选择器但结果不稳定，难以作为合并硬门禁，暂不采用。

## 配置契约

- `pipeline.browser.enabled`：是否启用。
- `start_command`：在批准 worktree 中启动项目的命令。
- `base_url`：健康检查和页面访问地址。
- `scenarios`：外置场景列表；每项包含 `name`、`path` 和 `actions`。
- 支持 `click`、`fill`、`press`、`wait_for`、`expect_visible`、`expect_text`、`expect_url`。
- 截图策略为 `on_failure`、`always` 或 `never`，默认 `on_failure`。

## 安全边界

- 场景文件和截图只写入 `runs/<task_id>/browser/`。
- 不记录 fill 的明文值到事件或 verification 摘要。
- 服务进程无论成功、失败或超时都必须清理。
- 浏览器依赖缺失时给出明确安装命令，不静默跳过已启用的门禁。
