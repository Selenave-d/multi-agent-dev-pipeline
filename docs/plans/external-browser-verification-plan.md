# 外置浏览器验收实现计划

## 改动文件

- `src/dev_pipeline/browser.py`：Playwright 驱动、服务生命周期、场景执行和截图。
- `src/dev_pipeline/execution.py`：在 lint/test/build 通过后执行 browser verification。
- `src/dev_pipeline/cli.py`：解析 `pipeline.browser` 并构造 verifier。
- `pyproject.toml`：增加可选 `browser` 依赖。
- `config.example.json`、`README.md`、`docs/providers.md`：配置与排障说明。
- `tests/test_browser.py`、`tests/test_execution.py`、`tests/test_cli.py`：逻辑与接线覆盖。

## 测试策略

- 用 fake page 覆盖点击、填充、键盘、可见性、文本和 URL 断言。
- 用 fake browser verifier 覆盖验证成功、失败及前置命令失败时不执行浏览器。
- 验证配置未启用时保持兼容，启用但字段缺失时尽早报错。
- 全量运行 pytest、Ruff、compileall 和 diff check，再交叉审查。

## 边界情况

- 服务启动超时、Playwright 未安装、Chromium 未安装。
- selector 不存在、断言失败、页面 JavaScript 异常。
- 场景名导致路径穿越、截图泄露输入值、服务子进程残留。

## 验收标准

- 业务仓库不产生任何测试文件。
- browser 步骤结果写入 `06_verification.json`；失败使状态进入 `needs_revision`。
- 截图和服务日志保存在当前 task 的 run 目录。
- 未配置 browser 的现有 52 项测试行为不变。
