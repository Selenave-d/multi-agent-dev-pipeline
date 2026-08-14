# Development worktree 与可观测性需求

## 问题

- Claude 直接生成 unified diff 时容易产生行号或上下文损坏，重试仍不稳定。
- pipeline 运行期间只能看到最终状态，无法判断模型调用、重试、校验和命令执行到了哪里。

## 目标

1. Claude development 在隔离、干净的临时 Git worktree 中直接编辑文件。
2. 由 Git 生成标准 diff，再转换为既有 `03_code_changes.json` 契约并交给 PatchValidator。
3. 每个 run 持久化结构化 `events.jsonl`，记录阶段、尝试、校验、worktree 和工具调用事件。
4. 工具 stdout/stderr 单独保存并默认脱敏；事件只引用日志路径。
5. 新增 `dev-pipeline logs --task-id <id> [--follow]` 查看历史或实时事件。

## 安全边界

- development worktree 从目标仓库当前 HEAD 创建，完成或失败后均清理。
- Claude 只开放读取、搜索、编辑和写文件工具，不开放 Bash。
- 主工作区必须干净；模型不得直接修改主工作区。
- approve、验证、merge 的人工门禁保持不变。
- 不记录或展示模型隐藏思维链。

## 验收

- Claude 返回结构化状态，实际 changes/diff 由 Git 生成且可通过 `git apply --check`。
- 新文件、多文件和无修改场景均有测试。
- 阶段开始、重试、失败/完成和工具输出路径可在事件日志中追踪。
- `logs` 可读取既有事件，`--follow` 可等待新增事件直到任务终态。
