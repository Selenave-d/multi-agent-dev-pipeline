# Provider 配置与排查

## 配置阶段工具

复制 `config.example.json` 为 `config.json`。`config.json` 已被 Git 忽略。

```json
{
  "providers": {
    "requirement": {"type": "zentao", "config_file": "~/.zentao.json"},
    "analysis": {"type": "kimi", "command": "kimi"},
    "development": {"type": "claude", "command": "claude", "model": "sonnet"},
    "review": {"type": "codex", "command": "codex"}
  }
}
```

支持范围：

| 阶段 | `type` 可选值 |
|---|---|
| requirement | `file`、`zentao` |
| analysis | `demo`、`kimi` |
| development | `demo`、`claude` |
| review | `demo`、`kimi`、`claude`、`codex` |

每个阶段可设置 `command`、`model`、`timeout_seconds`。`project.root` 必须指向待分析的真实业务仓库。

Claude development 不再手工输出 unified diff。Pipeline 从目标仓库 HEAD 创建临时 detached worktree，Claude 通过受限的 Read/Edit/Write/Glob/Grep 工具直接编辑文件，随后 Pipeline 用 `git diff --binary HEAD --` 生成 `03_code_changes.json`。临时 worktree 无论成功或失败都会清理。

Claude 编辑后会先保存 `development.raw.patch`，再对 changed files 执行内置末尾换行规范化。若项目存在本地 Prettier、Ruff、Black 或 gofmt，Pipeline 会自动调用；不会使用 npx 下载依赖。格式化后的最终结果保存为 `development.patch`。显式覆盖或关闭外部 formatter：

```json
{
  "pipeline": {
    "commands": {
      "format": "npm run format -- {files}"
    }
  }
}
```

`format: null` 只关闭外部 formatter，内置末尾换行规范化仍会执行。formatter 若修改 Claude 原变更集之外的文件，会以 `unexpected_format_changes` 终止。

每个任务的可观察执行轨迹保存在 `events.jsonl`，模型 stdout/stderr 保存在 `tool-output/` 并进行凭据脱敏。它不包含模型隐藏思维链。实时查看：

```powershell
dev-pipeline logs --config config.json --task-id BUG-6810 --follow
```

使用禅道需求时，建议在项目配置中声明产品 code：

```json
{
  "project": {
    "root": "../target-project",
    "zentao_product": "DTS"
  }
}
```

配置后，需求适配器会查询需求所属产品的详情并精确比较 `product.code`。不一致时以 `product_mismatch` 终止，需求不会进入分析；匹配的 code 会写入需求产物的 `raw_data.product_code`。未配置时不做产品查询，保持原有行为。

执行配置：

```json
{
  "pipeline": {
    "runs_dir": "runs",
    "worktree_dir": "runs",
    "command_timeout_seconds": 1200,
    "commands": {
      "lint": "npm run lint",
      "test": "npm run test",
      "build": "npm run build"
    }
  }
}
```

- `worktree_dir` 必须与 `runs_dir` 指向同一目录；每个任务实际使用 `<worktree_dir>/<task_id>/worktree`。
- 命令按 lint、test、build 顺序执行，失败即停止，stdout/stderr 写入 `06_verification.json`。
- 某项无需执行时设置为 `null`，记录为 `skipped`。
- `approve` 前要求目标仓库干净且位于普通分支；`merge` 前再次检查主分支和基准提交没有变化。

Merge 成功后 Pipeline 会尝试同步 Obsidian 工作日志。默认依次读取项目级和全局 Claude memory 的 `obsidian-log-path.md`；也可显式覆盖或关闭：

```json
{
  "pipeline": {
    "work_log": {
      "enabled": true,
      "path": "D:/Obsidian/Work/Daily_Logs"
    }
  }
}
```

日志条目来自 requirement 标题，按 bug/story/其他写成“修复/实现/优化”，不会调用模型。找不到路径时记录 `work_log_skipped`；写入失败记录 `work_log_failed`。两者都不会改变已经成功的 `merged` 状态。

## 外置页面点击与 E2E 验收

浏览器驱动属于 Pipeline，不会在 `project.root` 中创建 Playwright/Cypress 文件。安装一次：

```powershell
python -m pip install -e ".[browser]"
python -m playwright install chromium
```

在 Pipeline 配置中启用：

```json
{
  "pipeline": {
    "browser": {
      "enabled": true,
      "start_command": "npm run serve",
      "base_url": "http://127.0.0.1:8080",
      "environment": {"HOST": "127.0.0.1", "PORT": "8080"},
      "screenshots": "on_failure",
      "scenarios": [
        {
          "name": "登录并进入首页",
          "path": "/login",
          "actions": [
            {"action": "fill", "selector": "#username", "value": "e2e-user"},
            {"action": "fill", "selector": "#password", "value": "e2e-password"},
            {"action": "click", "selector": "button[type=submit]"},
            {"action": "expect_url", "value": "/dashboard"},
            {"action": "expect_text", "selector": "h1", "text": "控制台"}
          ]
        }
      ]
    }
  }
}
```

支持 `click`、`fill`、`press`、`goto`、`wait_for`、`expect_visible`、`expect_text` 和 `expect_url`；后两者均使用包含匹配。没有配置 `scenarios` 时默认访问 `/` 做 smoke check，空 `actions` 表示只验证页面能够加载。浏览器步骤在 lint/test/build 没有失败后执行，因此三项命令全部设为 `null` 时可以只把 browser 作为验证门禁。结果写入 `06_verification.json`；失败截图及 `server.log` 位于 `runs/<task_id>/browser/`，任务进入 `needs_revision`。截图前会遮盖所有通过 `fill` 写入的字段；测试账号仍应使用专用的非生产凭据。

## 凭据

禅道推荐使用用户级 `~/.zentao.json`：

```json
{
  "base_url": "http://zentao.example.com/zentao",
  "code": "应用代码",
  "key": "API签名密钥",
  "account": "禅道账号"
}
```

也可使用 `ZENTAO_BASE_URL`、`ZENTAO_CODE`、`ZENTAO_KEY`、`ZENTAO_ACCOUNT`。不要把凭据放进项目配置或运行产物。

Kimi、Claude、Codex 分别通过它们自己的登录命令配置：

```powershell
kimi login
claude
codex login
```

## 运行

真实工具链：

```powershell
dev-pipeline --config config.json --requirement story:123
dev-pipeline --config config.json --requirement bug:456
dev-pipeline status --config config.json
dev-pipeline logs --config config.json --task-id STORY-123 --follow
dev-pipeline approve --config config.json --task-id STORY-123
dev-pipeline revise --config config.json --task-id STORY-123
dev-pipeline merge --config config.json --task-id STORY-123
```

离线回归：

```powershell
dev-pipeline --config config.demo.json --requirement examples/requirement.json
```

## 常见问题

- `provider_not_installed`：`command` 不在 PATH。先运行 `<command> --version`，或在配置中写可执行文件绝对路径。
- `provider_timeout`：提高该阶段的 `timeout_seconds`，并检查 CLI 是否卡在登录、升级或权限提示。
- `invalid_model_output`：模型没有严格返回 JSON。检查运行状态中的阶段错误；升级 CLI 后重新核对结构化输出参数。
- `invalid_generated_patch`：Git 生成的 diff 未通过一次性校验 worktree 中的 `git apply --3way --ignore-space-change`。Pipeline 会把错误反馈给下一次生成；最新候选始终保存在 `development.patch`，全部重试失败时不会保存 `03_code_changes.json`。
- `zentao_auth_failed`：确认 `key` 是 API 应用签名密钥，不是网页登录密码；检查服务器时间差。
- `zentao_unreachable`：确认 VPN、代理、内网 DNS 和 `base_url`。
- 已有失败运行：修复问题后使用 `--resume`；Pipeline 会校验并复用已完成阶段。
- `project_dirty`：批准前目标仓库有未提交改动，先处理这些改动，避免 worktree 基准不明确。
- `browser_provider_not_installed`：安装 `.[browser]`，再执行 `python -m playwright install chromium`。
- `browser_server_timeout`：确认 `start_command` 能在 worktree 中启动服务，并且监听地址与 `base_url` 一致。
- `browser_assertion_failed`：查看 `06_verification.json`、`runs/<task_id>/browser/*.png` 和 `server.log`。
- `worktree_exists`：上一次 development 进程异常退出并留下 `runs/<task_id>/development-worktree`。确认没有需要保留的内容后，用 `git worktree remove --force <path>` 和 `git worktree prune` 清理，再执行 `--resume`。
- `base_commit_changed`：批准后主分支发生变化；当前实现拒绝直接合并，需在新基准上重新执行。
- `git_command_failed`：检查 `changes.patch`、Git 输出和目标文件是否与分析时一致。
- `needs_revision`：运行 `revise`，验证错误和 `06_verification.json` 会传回开发 Agent。
- Review 与开发不应共用隐式会话。当前所有 CLI 调用都是新会话，并显式传递 JSON 产物。

analysis 和 development 都必须返回 `change_status`。当前代码已满足需求时使用 `already_satisfied` 和空 `changes`，任务最终进入 `no_changes_needed`，不会开放 approve；确需修改时使用 `changes_required`，diff 必须在 development 阶段通过只读补丁校验。
