# ADR-0021：可分离 daemon、可重新附着 TUI 与远程伴侣

- 状态：Accepted
- 日期：2026-09-02
- 决策者：Bryant Yang

## 1. 背景

普通 `ChatApp` 同时持有 room lease、Orchestrator、CommandBus、ControlServer、
权限弹窗和 agent 进程。关闭 TUI 必然取消任务并回收 runtime，因此长任务无法在
终端关闭后继续，也无法从另一台已授权设备观察或控制。

直接让 Web/TUI 各自创建 Orchestrator 会破坏 room 单写者、同 session 单 writer、
权限归属和 no-replay。把现有 Unix socket 暴露到网络同样不成立：它没有网络认证，
且包含本机 owner 生命周期操作。

## 2. 决策

### 2.1 Owner 与客户端分层

- 保留 `myagents <workdir>`：普通 TUI 仍是前台 owner，退出即完整回收。
- 新增 `myagents daemon start|run|status|stop`。一个 `DaemonRuntime` 只持有一个
  `(workdir, session_name)` room，是该房间 lease、Orchestrator、CommandBus、
  ControlServer、权限等待和 agent 进程的唯一 owner。
- `myagents attach` 和 remote companion 都是无状态控制客户端，只能使用
  `ControlClient`；不得构造 RoomStore、Orchestrator、CommandBus 或 agent adapter。
  客户端退出不发送 shutdown、cancel 或 permission resolve。
- daemon 通过受信 control endpoint 关闭，不用 PID 文件强杀。SIGINT/SIGTERM 也
  进入同一有界关闭顺序：权限 fail-closed → 停止控制入口 → 取消 bus → 回收 agent。

### 2.2 附着 TUI

- attach 恢复同一持久 timeline、当前/排队任务和未解决权限请求。
- `Esc`/`Ctrl+X` 精确取消当前 running；无 running 时取消最早 queued。关闭界面
  只是 detach。
- 权限 broker 只向客户端投影脱敏工具标题、tool call id 和服务端本次 option
  闭集；原始工具参数不离开 owner。重新附着后只能选择原请求的精确 option；未知、
  过期或关闭竞态一律 cancelled/拒绝。

### 2.3 Remote companion

- `myagents remote` 只连接现存 daemon，并只监听 `127.0.0.1` 或 `::1`。不得直接
  绑定 `0.0.0.0`、LAN 或公网；跨设备使用 Tailscale Serve 等受信反向代理。
- HTTP API 使用至少 32 位随机 Bearer token。token 存在私有状态目录的 0600
  普通文件，目录 0700，拒绝 symlink；浏览器从 URL fragment 读取后立即移除，
  只放 sessionStorage，不使用 query、cookie、localStorage 或 CORS。
- Host header 默认只接受 loopback；反向代理域名必须通过 `--allowed-host` 精确
  opt-in。请求体上限 128 KiB，响应 `no-store`，浏览器以 `textContent` 渲染
  agent 内容。
- remote 可读 room/timeline/events/command 状态，可提交带 `request_id` 的命令、
  精确取消和调用既有有界 steering。它只能查看并**拒绝**权限；没有 approve、
  runtime shutdown、任意 control method passthrough 或权限模式切换路由。
- Web 输入补全从 `room.get.agents` 派生当前 worker/readiness 候选并附加 host，
  支持连续点名、可用项优先且保留未就绪项，不在前端维护第二份 agent 注册表。
  任务详情只经显式只读
  `command.events(command_id, limit)` 读取存储层已有的有界首尾事件投影；界面隐藏
  partial 正文和 thought，只呈现阶段、工具、权限、控制、错误与输出统计。内存
  command 列表因 daemon 重启为空时，timeline 已有的 `command_id` 只用于补出最近
  四个已结束或上次中断任务；状态由持久事件末态判定，缺少终态时明确显示中断，
  详情仍回到同一持久事件事实源读取。
- remote token 不是 room lease 或 agent 凭据。网关不读取模型/agent secret，
  不自动启动 daemon，也不自动 fallback。

### 2.4 提交、恢复与 no-replay

- 所有输入仍由 owner 内唯一 CommandBus 接受；`request_id` 幂等边界不变。
- 客户端断线只造成观察中断，不改变已提交 command。重连按 timeline seq 增量读取，
  不重发原输入。
- daemon 优雅停止将 active/queued terminalize 为 cancelled 并回收 adapter。
  进程遭 SIGKILL 时，已有 cursor/no-replay checkpoint 仍禁止自动重投；重启只把
  无活 owner 的非终态显示为上次中断，不恢复不确定执行。

## 3. 明确不做

- daemon 首版不同时持有多个 room；每个命名会话单独启动。
- 不实现公网账户系统、多人 ACL、远程批准写工具、A2A 或 Streamable HTTP MCP。
- 不把 `/yolo` 变成远程授权通道，也不承诺应用层权限等于 OS sandbox。

## 4. 验收

- `tests/test_runtime_daemon.py`：后台任务跨 detach/reattach、CLI
  start/status/stop、权限弹窗退出后仍等待、重新附着精确选择、Esc 精确取消且
  daemon 继续存活。
- `tests/test_remote_control.py`：Bearer/Host/loopback/请求上限、命令控制、
  command 详情、approve/shutdown 反例、deny-only 权限、0600 token/symlink、
  DOM 文本渲染。
- `tests/test_m3_control.py`：owner kind、command list、权限 broker、socket 生命周期。
- R7 AST gate：attach/remote 不得导入 owner 层；remote 必须保留 loopback、
  deny-only、无 approve/shutdown 路由。

## 5. 后果

优点是长任务生命周期与终端窗口解耦，且所有客户端复用现有单写者和 no-replay
契约。代价是 daemon 模式的多会话切换需要分别启动/附着，远程端不能批准工具；
这两个限制用于保持首版边界可审计。
