# Development worktree 与可观测性实现计划

1. 扩展 RunStore：追加脱敏事件、保存脱敏 stdout/stderr、读取事件。
2. 扩展命令运行器：捕获命令、退出码、耗时和原始输出并交给 RunStore。
3. 改造 ClaudeCodeClient：创建 detached worktree、允许受限编辑、用 Git 生成 diff、始终清理。
4. build_agents 按 task 注入 runs 路径、事件记录器和工具输出记录器。
5. Orchestrator 记录 run、stage、attempt、validation、retry、failure 和 completion 事件。
6. CLI 增加 `logs --task-id <id> [--follow]`，更新配置示例和 provider 文档。
7. 测试 worktree diff、清理、no-op、日志脱敏、阶段事件和 logs 输出。
8. 运行全量 pytest、Ruff、compileall、diff check 和跨模型 Review。

## 边界情况

- 主仓库脏、非 Git 仓库、Claude 未产生修改、声明 no-op 却实际修改。
- Claude 创建新文件；二进制或多文件 diff。
- 命令失败和超时仍需留下 stderr/stdout 与失败事件。
- 旧任务没有 events.jsonl 时 logs 返回空结果，不破坏状态读取。
