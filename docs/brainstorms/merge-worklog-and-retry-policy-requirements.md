# Merge 工作日志与重试策略需求

## 目标一：merge 后同步 Obsidian 工作日志

- merge 已成功并保存 `merged` 状态后，尽力把业务结果写入当日工作日志。
- 路径按显式 `pipeline.work_log.path`、项目级 Claude memory、全局 Claude memory 顺序发现。
- 无路径时非交互跳过；写入失败不能回滚或覆盖已经成功的 merge。
- 条目使用 `修复/实现/优化: 标题`，不出现模型名或 AI 字样。
- 创建标准日期标题和项目节；已有项目节时把条目插入该节，而不是错误追加到其他项目节。

## 目标二：确定性错误立即失败

- `PipelineError` 默认保持可重试，避免破坏 provider 超时、命令失败和补丁生成反馈闭环。
- `ValidationError` 一律不可重试。
- 产品不匹配、工作区脏、路径逃逸、formatter 扩散改动和浏览器端口占用显式不可重试。
- Orchestrator 的失败事件必须准确记录 `will_retry=false`，随后沿用现有失败收尾。

## 设计取舍

- 工作日志采用独立 `WorkLogWriter`，而不是把路径解析和 Markdown 操作塞进 executor，便于纯逻辑测试。
- 日志内容使用确定性压缩和敏感词清理，不引入新的模型调用。
- 不给错误码维护第二份全局不可重试清单，避免异常定义和 Orchestrator 策略漂移。
