# Merge 工作日志与重试策略实现计划

## 改动文件

- `src/dev_pipeline/worklog.py`：路径发现、内容生成、Markdown 插入、原子写入和读回确认。
- `src/dev_pipeline/execution.py`：merged 状态持久化后尽力同步并记录事件。
- `src/dev_pipeline/cli.py`：读取和校验 `pipeline.work_log`。
- `src/dev_pipeline/errors.py`、`orchestrator.py`：异常重试属性和快速失败分支。
- `providers.py`、`patches.py`、`formatting.py`、`browser.py`：确定性错误标记。
- `config.example.json`、`docs/providers.md`：配置和事件说明。
- `tests/test_worklog.py`、`test_execution.py`、`test_pipeline.py`：完整行为覆盖。

## 测试策略

- 固定本地时间测试新文件、已有节、新建节、三种动词和禁止词清理。
- 使用临时 home 测试显式、项目级、全局三级优先级和无路径跳过。
- fake writer 抛错验证 merge 仍为 `merged` 且产生 `work_log_failed`。
- 三类 agent 异常分别验证不可重试 PipelineError、默认 PipelineError、ValidationError 的 attempts。

## 边界情况

- memory 文件存在但没有合法反引号目录。
- 日志目录不存在、文件已有其他项目节、标题只包含被禁止词。
- merge 状态已经保存后加载 requirement 或写日志失败。
- 生成补丁与开发结果不一致仍必须保留原有重试次数。

## 验收标准

- merge 成功后正确写入或产生 skipped/failed 事件，任何日志问题都不改变 `merged`。
- 明确不可重试错误 attempts 仅增加一次；默认错误仍执行 `max_retries + 1` 次。
- 全量 pytest、Ruff、compileall、diff check 和交叉审查通过。
