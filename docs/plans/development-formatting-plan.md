# Development 自动格式化实现计划

1. 新增 `DevelopmentFormatter`，负责路径校验、末尾换行规范化、工具探测和命令执行。
2. Claude client 在格式化前保存 `development.raw.patch`，格式化后保存 `development.patch`。
3. 比较格式化前后的 Git changed files，拒绝修改集合扩散。
4. 从 `pipeline.commands.format` 读取可选覆盖；未声明时自动探测，显式 null 时只做内置规范化。
5. 记录 format started/completed/skipped/failed 事件及脱敏工具输出。
6. 覆盖缺少末尾换行、raw/final patch、无 formatter 降级、无关文件扩散、二进制和路径安全测试。
7. 运行全量 pytest、Ruff、compileall、diff check 和跨模型 Review。
