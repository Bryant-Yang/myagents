# ADR-0005：同一项目的独立会话

- 状态：Accepted
- 日期：2026-07-28
- Owner：Bryant Yang
- 里程碑：M4.2

## 1. 背景

当前房间身份只由规范化 `workdir` 决定，因此关闭并重新启动 TUI 会恢复同一条
timeline、agent cursor 和原生 ACP/app-server session。用户无法在保留旧历史的
同时，为同一项目开始一段完全独立的对话。

通过修改 `XDG_STATE_HOME` 可以绕开，但这会把实现细节暴露给用户，也让 MCP
bridge、恢复命令和房间身份难以保持一致。

## 2. 决策

### 2.1 房间身份

- 房间身份改为 `(规范化 workdir, session_name)`。
- `session_name="default"` 保持原 `room_id = sha256(workdir)[:16]`，现有房间
  无迁移、无复制、无丢失。
- 非默认会话使用带分隔域的哈希输入，得到独立且不可逆的 `room_id`。
- `session_name` 为 1–64 个去除首尾空白后的可见字符；拒绝控制字符、`/`、
  `\`、`.` 和 `..`。目录仍只使用哈希，不使用用户输入作为路径。
- 新建房间的 `state.json` 记录 `session_name`；旧默认房间缺少该字段时按
  `default` 兼容读取。命名房间缺失或不匹配必须 fail loudly。

### 2.2 TUI 新建与切换

- `Ctrl+N` 或精确输入 `/new` 打开命名弹窗；`/new` 是 TUI 本地命令，不写入
  timeline、不触发 host 路由。空名称自动生成
  `chat-<UTC timestamp>-<short id>`。
- 已存在的名称不覆盖、不清空，提示用户改名；恢复已有会话使用启动参数
  `--session NAME`。
- 有 active command 或待处理权限时拒绝新建，用户应先完成或取消当前任务。
- 切换不在活跃 App 内偷换 Orchestrator。当前 App 通过正常 unmount 顺序关闭
  control server、CommandBus、adapter 和 lease，再由同一进程的顶层循环创建
  新 App。旧会话状态保持原样。

### 2.3 外部入口

- `ControlClient` 和 `myagents_mcp.py` 增加同一个可选 `session_name/--session`
  选择器，默认仍为 `default`。
- endpoint 继续放在会话对应的 room 目录；控制协议版本不因路径选择器改变。
- `room.get` 返回 `session_name`，让外部调用方确认自己连接的会话。

## 3. 不选择的方案

- **删除默认房间状态实现“新会话”**：不可恢复且会破坏旧历史。
- **在同一 Orchestrator 上清空 history/cursor**：原生 agent session 仍含旧
  上下文，会产生“界面空了但模型记得”的假新会话。
- **Ctrl+N 启动第二进程**：违反单写者和上层不直接启动进程的边界。
- **继续要求用户切换 XDG_STATE_HOME**：无法形成稳定的产品级会话身份。

## 4. 验收

1. 默认会话继续打开现有 room_id 和历史。
2. 同一 workdir 的两个命名会话具有不同 room_id、timeline、events、cursor
   和 agent session 映射；重开同名会话可恢复。
3. 非法名称、state 中名称不匹配、同名新建和活跃任务切换均明确失败。
4. `Ctrl+N` 经正常 unmount 后启动新 App；旧房间 endpoint、进程和 lease
   均已收尾。
5. `--session NAME` 与 MCP `--session NAME` 指向同一命名房间。
6. storage、M2.5、M3 control/MCP、TUI 与完整 Harness 门禁通过。
7. 精确 `/new` 与 `Ctrl+N` 同义且不持久化、不路由；其他普通文本不做本地
   语义猜测。

## 5. 后果

同一项目可以保留多段真正隔离的 agent 对话。当前版本只提供“新建”和按名称
启动恢复，不在 TUI 内提供会话列表、删除或重命名；这些操作需要单独设计可恢复
语义后再增加。
