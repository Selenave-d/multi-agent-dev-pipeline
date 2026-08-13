# 工具调用方式调研

本文记录 Pipeline 适配器依赖的非交互调用方式。命令以 2026-08-13 本机安装版本和官方文档为准；升级 CLI 后应重新运行 `--help` 核对参数。

## Kimi Code

本机命令：`kimi`，实测版本 `0.34.0`。

```powershell
kimi -p "分析当前需求" --output-format stream-json
kimi --model <模型别名> -p "分析当前需求" --output-format stream-json
```

- `-p/--prompt`：非交互执行一次 prompt 后退出。
- `--output-format stream-json`：stdout 每行一个 JSON 对象。
- 普通回复形如 `{"role":"assistant","content":"..."}`；调用工具时会夹杂 assistant/tool 消息。
- 诊断和进度写入 stderr。适配器取最后一条带字符串 `content` 的 assistant 消息，再解析其中的 JSON。
- 首次使用运行 `kimi login`。CLI 登录令牌由 Kimi 自己保存，Pipeline 不读取令牌。

分析 Agent 的 prompt 明确要求只读分析、禁止写代码和执行命令。Kimi 的 prompt 模式自身使用自动权限策略，因此应仅在可信目标仓库中运行。

官方参考：[Kimi command](https://moonshotai.github.io/kimi-code/en/reference/kimi-command)

## Claude Code

本机命令：`claude`，实测版本 `2.1.186`。

```powershell
claude -p "生成代码补丁" `
  --output-format json `
  --json-schema '<JSON Schema>' `
  --tools "" `
  --no-session-persistence
```

- `-p/--print`：非交互执行。
- `--output-format json`：返回一个 JSON envelope。
- `--json-schema`：约束结构化输出；适配器优先读取 `structured_output`，兼容从 `result` 字符串解析。
- `--tools ""`：不给开发 Agent 工具，防止它直接改工作区；它只根据 Pipeline 提供的源文件内容生成 unified diff。
- `--no-session-persistence`：不保留独立 CLI 会话。
- 可用 `--model sonnet` 或完整模型 ID 指定模型。

认证由 Claude Code 自身配置管理。官方 CLI 也支持 API Key；若使用 `--bare`，仅从 `ANTHROPIC_API_KEY` 或指定设置读取认证。本项目不把密钥写入 `config.json`。

官方参考：[Claude Code CLI reference](https://docs.anthropic.com/en/docs/claude-code/cli-usage)

## Codex Review

本机命令：`codex`，实测版本 `0.144.1`。Review 适配器使用通用 `exec`，因为它支持 JSON Schema 和最终消息文件：

```powershell
Get-Content prompt.txt | codex exec `
  -C <项目目录> `
  --sandbox read-only `
  --ask-for-approval never `
  --ephemeral `
  --output-schema review.schema.json `
  --output-last-message review.json `
  -
```

- `--sandbox read-only`：审查过程不能改代码。
- `--output-schema`：约束最终审查 JSON。
- `--output-last-message`：避免从事件流中猜测最终结果。
- `--ephemeral`：不保留会话。
- Codex 也有 `codex review --uncommitted/--base/--commit`，更适合审查已实际应用到 Git 工作区的变更；当前 Pipeline 审查的是 `03_code_changes.json` 中尚未应用的 diff，因此使用 `exec`。

认证由 `codex login` 或 Codex 桌面端维护，Pipeline 不读取凭据。

## 禅道 21.x

沿用现有 `zentao-bugfix` Skill 的认证协议和详情接口：

1. 从 `~/.zentao.json` 读取 `base_url/code/key/account`，或使用同名 `ZENTAO_*` 环境变量。
2. 生成 Unix 秒级时间戳。
3. `token = md5(code + key + timestamp)`。
4. GET `api.php?m=user&f=apilogin&account=...&code=...&time=...&token=...`。
5. 从 `Set-Cookie` 提取 `zentaosid`。
6. 携带 Cookie 查询：
   - 需求：`/story-view-<id>.json`
   - Bug：`/bug-view-<id>.json`

禅道旧接口可能返回 `{"status":"success","data":"<JSON字符串>"}`，需要二次 JSON 解码。适配器是只读 GET，不做状态、评论或需求正文写回。

标准引用格式：`story:123`、`bug:456`；纯数字默认按需求处理。
