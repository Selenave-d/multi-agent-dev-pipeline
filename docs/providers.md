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
- `invalid_generated_patch`：development 生成的 diff 未通过目标仓库中的 `git apply --check`。Pipeline 会把错误反馈给下一次生成；全部重试失败时不会保存 `03_code_changes.json`。
- `zentao_auth_failed`：确认 `key` 是 API 应用签名密钥，不是网页登录密码；检查服务器时间差。
- `zentao_unreachable`：确认 VPN、代理、内网 DNS 和 `base_url`。
- 已有失败运行：修复问题后使用 `--resume`；Pipeline 会校验并复用已完成阶段。
- `project_dirty`：批准前目标仓库有未提交改动，先处理这些改动，避免 worktree 基准不明确。
- `base_commit_changed`：批准后主分支发生变化；当前实现拒绝直接合并，需在新基准上重新执行。
- `git_command_failed`：检查 `changes.patch`、Git 输出和目标文件是否与分析时一致。
- `needs_revision`：运行 `revise`，验证错误和 `06_verification.json` 会传回开发 Agent。
- Review 与开发不应共用隐式会话。当前所有 CLI 调用都是新会话，并显式传递 JSON 产物。

analysis 和 development 都必须返回 `change_status`。当前代码已满足需求时使用 `already_satisfied` 和空 `changes`，任务最终进入 `no_changes_needed`，不会开放 approve；确需修改时使用 `changes_required`，diff 必须在 development 阶段通过只读补丁校验。
