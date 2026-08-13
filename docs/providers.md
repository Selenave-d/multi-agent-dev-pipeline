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
```

离线回归：

```powershell
dev-pipeline --config config.demo.json --requirement examples/requirement.json
```

## 常见问题

- `provider_not_installed`：`command` 不在 PATH。先运行 `<command> --version`，或在配置中写可执行文件绝对路径。
- `provider_timeout`：提高该阶段的 `timeout_seconds`，并检查 CLI 是否卡在登录、升级或权限提示。
- `invalid_model_output`：模型没有严格返回 JSON。检查运行状态中的阶段错误；升级 CLI 后重新核对结构化输出参数。
- `zentao_auth_failed`：确认 `key` 是 API 应用签名密钥，不是网页登录密码；检查服务器时间差。
- `zentao_unreachable`：确认 VPN、代理、内网 DNS 和 `base_url`。
- 已有失败运行：修复问题后使用 `--resume`；Pipeline 会校验并复用已完成阶段。
- Review 与开发不应共用隐式会话。当前所有 CLI 调用都是新会话，并显式传递 JSON 产物。
