# ADR-0001：持久房间、单写者 command bus 与本机 MCP 入口

> 房间身份在 2026-07-28 由
> [ADR-0005](0005-project-conversation-sessions.md) 扩展为
> `(workdir, session_name)`；本 ADR 中只按 workdir 描述的条款适用于
> `session_name="default"` 的兼容房间。

- 状态：Accepted
- 日期：2026-07-26
- Owner：Bryant Yang
- 里程碑：M2.5、M3

## 1. 背景

M2 的 `Orchestrator` 只在内存中维护共享 history、ACP cursor 和 session。
TUI 重启会丢失房间状态；另一个 coding agent 也只能靠人工复制文本，无法把
review 发送进正在运行的统一 TUI。

外部入口不能直接创建第二个 `Orchestrator` 或写入已有 ACP session。否则同一
session 出现多个 writer，TUI 看不到外部消息，权限请求也可能绕过当前 UI。

## 2. 决策

### 2.1 房间身份与状态位置

- 一个规范化绝对 `workdir` 对应一个稳定 `room_id`；默认 room 名为 workdir
  的 basename，`room_id` 不包含可逆的完整路径。
- 默认状态根目录为
  `${XDG_STATE_HOME:-~/.local/state}/myagents`，测试可显式注入临时
  `state_root`。状态不得写进目标工作区。
- 每个房间目录至少包含：
  - `timeline.jsonl`：append-only 时间线；
  - `state.json`：schema version、规范化 workdir、ACP cursor/session 映射；
  - `control.sock`：TUI 运行期间存在的本机控制 socket；
  - `endpoint.json`：供本机 MCP bridge 发现活跃房间。
- 房间目录权限为 `0700`，状态文件为 `0600`；写 `state.json` 和
  `endpoint.json` 使用同目录临时文件 + `os.replace` 原子替换。

### 2.2 时间线与 checkpoint

- 时间线记录包含单调递增 `seq`、`speaker`、`text`、UTC `created_at`，
  以及可选的 `command_id`；文本与单条记录均有明确大小上限。
- 用户消息、agent 最终回复、调用失败都先 append 并 flush，再向调用方确认。
- cursor 表示“该 agent 不应自动重放到哪个 timeline `seq`”，不再依赖易漂移
  的 list 下标。prompt 成功后推进；提交前或服务端明确拒绝的失败保持旧值；
  prompt 已提交后若结果不确定，则先持久化 no-replay cursor 再公开失败。
- `state.json` 保存每个 stateful agent 的 cursor 和 ACP `session_id`。写入
  失败不得伪装成功；不会把仅存在于内存的 cursor 当成已提交状态。
- 读取时间线必须有 `after_seq` + `limit` 边界，默认 50，最大 200，并返回
  `has_more`、`next_after_seq`。

### 2.3 ACP session 恢复

- ACP adapter 暴露通用的 session restore 能力；差异不能按 agent 名进入
  `Orchestrator`。
- 启动后若存在记录的 `session_id`，优先调用 `session/load`。成功后继续从
  已持久化 cursor 增量发送。
- `session/load` 不支持或失败时创建新 session，并把该 agent cursor 重置为
  bootstrap 起点；下一轮按 `history_limit` 有界补上下文，不能沿用旧 cursor
  静默跳过 history。
- 新建/恢复成功后立即持久化实际 session id。adapter 进程重建导致 session
  变化时同样更新映射。
- 一个运行中的 TUI 是房间 Orchestrator 与其 ACP session 的唯一 writer。

### 2.4 内部 command bus

TUI 进程拥有一个与 UI 无关的 in-process `CommandBus`，控制 socket 只负责
序列化调用。外部消息必须调用同一个 `Orchestrator.dispatch`，不能操作
adapter/session，也不能直接写 timeline。

最小命令：

| 命令 | 语义 |
| --- | --- |
| `room.get` | 返回房间、workdir、活跃状态、agent transport |
| `timeline.read` | 按 `after_seq` / `limit` 读取持久时间线 |
| `command.submit` | 提交一条外部用户消息，立即返回 `command_id` |
| `command.get` | 返回 queued/running/completed/failed/cancelled 状态 |
| `command.wait` | 有界等待状态变化，超时不取消实际任务 |

约束：

- `command.submit` 的 message 非空且有大小上限；`request_id` 可选，若提供则在
  房间内实现幂等去重。
- command 按提交顺序进入队列。一个 command 可在 Orchestrator 内 fan-out；
  不允许两个 command 并发修改共享 history。
- fan-out 等待全部 target 收尾；任一 worker 失败时 command 终态为 `failed`，
  但不取消其他 target。失败 worker 的 partial 必须在 timeline 明确标注失败。
- command 状态与时间线写入使用同一事件循环；TUI 更新通过线程安全/事件循环安全
  callback 进入 RichLog。
- 权限请求仍由当前 TUI 决策；MCP 与 socket 都没有 `auto` 放行入口。
- TUI 退出先停止接收新命令，再让队列收尾或标记 cancelled，随后关闭 adapters
  并删除 endpoint/socket。

### 2.5 本机控制 socket

- 使用 Unix domain socket + 每行一个 JSON request/response。它是私有本机
  transport，不对外宣称为 MCP。
- 仅绑定 ADR 定义的房间路径；socket 权限 `0600`。启动时只清理已确认无监听者
  的 stale socket/endpoint，不能抢占活跃房间。
- 每个请求和响应有大小上限；未知命令、非法字段和超时返回稳定错误码，不泄露
  traceback。
- `endpoint.json` 只登记当前 PID、room_id、规范化 workdir、socket path 和
  protocol version；发现后仍必须实际连接验证。

### 2.6 MCP bridge

- `myagents_mcp.py` 是本机 stdio MCP server。它只连接活跃 TUI 的控制 socket，
  绝不实例化 `Orchestrator`。
- 使用官方 Python MCP SDK 稳定 v1，依赖固定为 `mcp>=1.27,<2`。stdout 只输出
  MCP 帧，日志只写 stderr。
- 工具名带 `myagents_` 前缀，输入由 SDK/Pydantic 校验，返回结构化数据及兼容
  文本。MCP tool execution error 使用可操作错误信息。

| MCP tool | command | 注解 |
| --- | --- | --- |
| `myagents_get_room` | `room.get` | read-only、idempotent、closed-world |
| `myagents_read_timeline` | `timeline.read` | read-only、idempotent、closed-world |
| `myagents_send_message` | `command.submit` | write、non-idempotent（有 request_id 时去重）、open-world |
| `myagents_get_command` | `command.get` | read-only、idempotent、closed-world |
| `myagents_wait_command` | `command.wait` | read-only、idempotent、closed-world |

M3 只支持显式 `--workdir` 选择一个本地房间。TUI 未运行、endpoint stale、房间
不匹配时返回明确错误和启动命令，不自动拉起 TUI。

## 3. 不选择的方案

- **MCP server 自己持有 Orchestrator**：会形成第二 writer，破坏权限和 UI
  可见性。
- **直接把 ACP session id 交给 Codex/Kimi**：TUI 不会自动看到消息，且会并发
  写坏 session。
- **Streamable HTTP**：M3 是单机单用户 CLI 集成，stdio 更符合标准，避免端口、
  Origin、认证与 DNS rebinding 面。
- **A2A**：当前没有跨机器、跨组织 agent-to-agent 协作需求。

## 4. 验收契约

### M2.5

1. 同一 workdir 重启 TUI 后时间线、单调 seq 和 agent cursor 恢复。
2. 记录的 ACP session 可 `session/load`，成功恢复后不重复发送旧 transcript。
3. load 仅在标准 resource/method-not-found 或具体 adapter 已获证的
   精确 session-not-found 映射时新建 session 并有界 bootstrap；其他
   remote/transport 错误 fail-closed，不用 fresh session 绕过。
4. timeline/state 损坏、schema 不支持、持久化写失败均 fail loudly，不能悄悄
   启动空房间覆盖旧数据。
5. 状态文件不污染 workdir，目录/文件权限符合本 ADR。

### M3

1. MCP 子进程调用 `myagents_send_message` 后立即获得 `command_id`；消息出现在
   已运行 TUI 的共享时间线，并由同一个 Orchestrator/ACP session 处理。
2. `get/wait/read` 可看到 queued → running → terminal 状态和 agent 最终回复；
   timeline pagination 不越界、不丢 seq。
3. 外部消息触发工具权限时，仍在 TUI 弹窗；bridge 无绕过路径。
4. 同房间并发 submit 按队列顺序执行；相同 `request_id` 不重复执行。
5. socket/endpoint 权限正确；第二个 TUI 不抢占；stale 文件可恢复；正常退出无
   socket、endpoint、MCP 或 agent 残留进程。
6. MCP 工具可由官方 SDK client 通过 stdio list/call；stdout 无非协议输出。
7. M0–M2、JSONL fallback、红线门禁全部无回归。
8. fan-out 任一 worker 失败时其他 target 仍完成，但 command 终态为 `failed`；
   已提交后结果不确定的 ACP timeout 建立 no-replay cursor。

## 5. 必需证据

- 单元/集成：持久化、恢复失败、queue/idempotency、socket 安全、MCP stdio。
- fake ACP：`session/load` 成功/失败与 session id 更新。
- Textual pilot：外部 command 实时显示、权限仍由 TUI 处理、退出回收。
- 真实 E2E：临时工作区启动 TUI + 真实 `kimi acp`，从独立 MCP client 提交一条
  无工具消息并读取完成结果；退出后检查无残留。
- 总门禁：`bash scripts/check-harness.sh`。

## 6. 后果

M2.5/M3 增加了持久化格式和一个本机 IPC 边界，但保住了最重要的单写者约束：
TUI 仍是唯一会话所有者。后续 GUI、Codex skill 或其他 MCP host 都能复用同一
入口；跨机器需求出现前，不引入远程认证和 A2A。
