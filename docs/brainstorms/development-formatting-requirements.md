# Development 自动格式化需求

## 目标

- Claude 编辑完成后、Git 生成最终补丁前，自动修复末尾换行等格式问题。
- 默认无需业务项目配置；优先使用项目已有的本地 formatter，找不到时只做内置文本规范化。
- formatter 只能修改 Claude 已经变更的文件，禁止扩散到无关文件。
- 同时保留 `development.raw.patch` 和格式化后的 `development.patch`。

## 探测优先级

1. `pipeline.commands.format` 显式命令；`null` 表示禁用外部 formatter。
2. 项目本地 Prettier。
3. 已安装且项目声明使用的 Ruff、Black 或 gofmt。
4. 无可靠 formatter 时仅确保 UTF-8 文本以换行结尾。

## 安全边界

- 不使用 npx 自动下载依赖。
- 命令以 argv 方式执行，不通过 shell 拼接 changed files。
- 删除文件和二进制文件不做文本规范化。
- formatter 新增其他 Git 变更时以 `unexpected_format_changes` 终止。
