# ADR-0007：OpenCode ACP-first、权限收口与受限 JSONL 降级

- 状态：Accepted
- 日期：2026-08-08
- 里程碑：M4.5
- 作者：Bryant Yang

## 1. 背景

本机 OpenCode 1.18.14 同时提供：

- `opencode acp`：ACP v1 stdio server；本机 wire probe 已确认
  `session/new`、`session/list`、`session/load`、`session/prompt`、
  `session/cancel`、权限反向请求、图片与 session resume capability。
- `opencode run --format json`：一次性 JSONL 事件流，旧生产 adapter 已支持。

OpenCode 官方权限文档说明大部分工具默认是 allow，不能把“ACP client 默认
deny”误当成 agent 一定会发权限请求；只有 OpenCode 自身把工具判定为 ask，
ACP server 才会把选择交给 myagents。另一方面，JSONL 无头模式没有 myagents
权限 UI，不能直接作为可写自动 fallback。

相关上游事实：

- [OpenCode Permissions](https://opencode.ai/docs/permissions)
- [OpenCode Agents](https://opencode.ai/docs/agents)
- [OpenCode Config](https://opencode.ai/docs/config)

## 2. 决策

### 2.1 生产 transport

OpenCode 生产注册改为：

```text
AgentSpec("opencode", "acp+jsonl", AcpOpenCodeAdapter)
```

`AcpOpenCodeAdapter` 复用通用 `AcpAdapter` 的 session、cursor、权限、取消、
no-replay、图片与 prepare-only fallback seam。Orchestrator 不出现 OpenCode
专用分支。

### 2.2 ACP 权限收口

启动 `opencode acp` 时通过子进程环境注入当前 CLI 已验证的
`OPENCODE_PERMISSION`：

- 未知工具 `*`：ask；
- `read` / `glob` / `grep` / `list` / `lsp` / `todowrite`：allow；
- `edit` / `bash` / `task` / `skill` / `webfetch` / `websearch` /
  `external_directory`：ask。

该 runtime override 的目的不是自动授权，而是确保有副作用及未知工具进入 ACP
`session/request_permission`，最后仍由 `AcpClient(permission="deny")` 或 TUI
选择决定。无 TUI handler、handler 异常、非法 optionId 一律 cancelled。

workflow 的 `read_only` 是独立 execution profile：runtime 改为 `*=deny`，只对
`read` / `glob` / `grep` / `list` / `lsp` / `todowrite` allow。不能沿用普通
轮次的 ask 再由 ACP client 回 cancelled；OpenCode 1.18.14–1.18.15 实测在
Bash 权限被 cancelled 后会直接 `end_turn` 且不产生正文，导致 verifier 无法
提交结果信封。
runtime hard deny 会把工具失败反馈给模型，使其继续使用安全读取并产出报告。

普通与 `read_only` profile 互相切换时必须关闭 ACP 进程、丢弃 resume id 并
`session/new`；不能在同一进程/session 中切换环境，也不能继承普通轮次可能获得的
`allow_always`。进入普通轮次后恢复 unknown/risky=ask，不能把只读 deny 策略
扩散到用户可交互授权的任务。

`OPENCODE_PERMISSION` 是本机 1.18.14 已验证的 runtime seam，CLI 升级后必须
通过 `opencode debug agent build` 和真实 ACP 权限探针复验，不能只看帮助文本。

### 2.3 JSONL 只读 fallback

自动 fallback 使用 `OpenCodeAdapter.readonly_fallback()`，并同时执行：

- `opencode --pure run ... --agent myagents-readonly-fallback`；
- `OPENCODE_DISABLE_PROJECT_CONFIG=1`，不加载项目 `opencode.json`、
  `.opencode` component/plugin 或项目 agent 覆盖；
- `OPENCODE_DISABLE_CLAUDE_CODE=1`，不加载 Claude 兼容层；
- `OPENCODE_DISABLE_AUTOUPDATE=1`，降级轮不自升级；
- `OPENCODE_CONFIG_CONTENT=<内置 profile>` 选择专用 primary agent；
- `OPENCODE_PERMISSION=<deny-all + read/glob/grep/list allow>` 在 runtime
  再次覆盖权限。

专用 agent 的唯一工具白名单是 `read` / `glob` / `grep` / `list`。未知工具、
写入、shell、网络、Skill、子 agent、MCP 与外部目录均 deny。prompt 约束只用于
要求回复诚实，不作为权限边界。

禁用项目配置会使 fallback 不自动加载项目 AGENTS；专用 prompt 要求它在存在时
用 read 显式读取 `AGENTS.md`。这是安全降级的代价，不影响正常 ACP 路径。

### 2.4 降级和 no-replay 时机

沿用 ADR-0006 的通用 seam：只有新 ACP 连接因本地 executable 无法启动，
或 initialize / `session/new` 明确返回标准 method-not-found（`-32601`），
且尚未建立 session、未发送 `session/prompt` 时，才自动切换 JSONL。

以下情况禁止 fallback：

- 活跃 session id 冲突；
- `session/load`、认证/权限/配额/backend 拒绝、timeout 或 transport 错误；
- checkpoint / make_prompt 失败；
- prompt 明确拒绝；
- prompt 写入后的断线、静默超时、取消或结果不确定。

后一类先固化 no-replay cursor，再公开失败。降级 checkpoint 使用
`fallback:jsonl:opencode`；下一轮不 load 伪 id，直接新建 ACP session 并有界
bootstrap。

## 3. 验收

1. 本机 capability probe 返回 ACP v1、`loadSession=true`、image 与
   list/resume capability。
2. 真实无害 Bash probe 产生 `session/request_permission`，options 为
   allow_once / allow_always / reject_once；默认 deny 后以 end_turn 结束。
3. contract test 证明生产注册为 `AcpOpenCodeAdapter`；普通 ACP policy 是
   unknown/risky=ask、read/search=allow；`read_only` 是 unknown/risky=deny、
   read/search=allow，硬拒绝工具后仍输出 workflow 信封，切回普通模式重建进程
   并恢复 ask。
4. JSONL contract 证明 `--pure`、隔离环境和专用 agent 生效；profile 精确为
   deny-all + read/glob/grep/list allow，且命令不含 `--auto`。
5. fake initialize 失败只调用一次 fallback，公开 prepare-only info；
   post-submit 断线零 fallback。
6. R4 负向探针能拦截直接 JSONL 注册、ACP 风险工具 allow 或 fallback 白名单
   放宽；清理探针后全量 Harness 通过。
7. 真实临时目录探针覆盖 ACP 正常回复、session/load、权限 deny 和受限 JSONL；
   结束后没有残留 `opencode acp` 进程。真实模型不进入默认 gate。
8. OpenCode 1.18.15 workflow-shaped 只读探针连续 5/5 在 Bash runtime deny 后
   改用安全读取并返回合法 verify/pass 信封；完整 Kimi → Codex → OpenCode →
   host 临时仓库 workflow 通过。该真实探针仍不进入默认 gate。

## 4. 后果

- Kimi 与 OpenCode 都成为 ACP-first stateful worker，支持共享时间线的增量投递、
  TUI 权限、取消、恢复和 no-replay。
- OpenCode 不再每轮启动 JSONL server；正常多轮复用 ACP 进程/session。
- OpenCode 默认 permissive 权限被生产 adapter 收口，但用户仍可在 TUI 对单次
  请求选择 allow_once / allow_always。
- OpenCode workflow 复核不会再因权限 cancelled 静默结束；代价是 execution
  profile 切换会建立 fresh ACP 进程/session，不保留跨 profile 原生上下文。
- fallback 能力有意只读；需要写入时返回阻塞说明，等待 ACP 恢复后继续。
- OpenCode CLI 的权限和配置环境变量可能漂移，升级后需重跑真实 probe。
