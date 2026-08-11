# ADR-0010：TUI 多会话管理与后台执行

- 状态：Accepted
- 日期：2026-08-11
- Owner：Bryant Yang
- 里程碑：M4.7

## 1. 背景

ADR-0005 让同一项目拥有多个隔离会话，但 TUI 只能新建会话；恢复旧会话必须
退出后传入 `--session`。当前 `ChatApp` 还把一个 Orchestrator、CommandBus、
权限 UI 和可见时间线绑定成一个生命周期，所以任务运行时无法切换会话。

这使会话身份已经存在于存储层，却没有形成可发现、可切换、可整理的用户体验。
长绝对路径形式的图片引用也会污染草稿、时间线和会话摘要。

## 2. 决策

### 2.1 稳定身份与可变标题

- 既有 `(workdir, session_name)` 和 `room_id` 继续作为稳定身份；现有 CLI、MCP、
  endpoint 与原生 agent session 映射不改变。
- `state.json` 增加可选展示元数据。旧房间缺少这些字段时，以 `session_name`
  作为标题并从 timeline 推导摘要，不做破坏性迁移。
- 新会话使用不可变的自动生成 `session_name`，初始标题为“新会话”。第一条用户
  消息经确定性清理和截断后成为标题；标题可修改，但不改变 room_id 或目录。
- 会话目录只从状态根的 `rooms/<room_id>/state.json` 发现。目录名、state 中的
  room_id、workdir 与 session_name 必须互相校验；损坏项不得被当作合法会话。

### 2.2 会话目录

- TUI 的 `Ctrl+O` 与精确 `/sessions` 打开会话选择器；默认当前项目，可切换到
  全部项目并按 workdir 分组。
- 列表按最后活动时间倒序，展示标题、项目、最后一条用户消息摘要、消息数与
  运行/未读状态。搜索只覆盖这些已展示元数据，不调用模型、不做全文索引。
- `Ctrl+N` 直接创建“新会话”；发送第一条消息后自动生成标题。
- 删除是明确的永久删除：运行中会话不得删除，当前会话必须先切走，并要求完整
  输入展示标题确认。删除前解析并校验唯一 room 目录，禁止宽泛路径或 glob。

### 2.3 多会话生命周期

- TUI 进程拥有一个 `SessionManager`。每个已加载会话仍有独立 RoomStore、lease、
  Orchestrator、CommandBus、ControlServer 与 agent runtime；不存在跨会话共享
  ACP/app-server writer。
- 切换只改变当前可见会话；后台 command、权限等待和持久化继续运行。agent 事件
  必须先绑定稳定 room_id，再进入 UI，禁止用“当前会话”反查归属。
- 权限弹窗是全局的，并明确显示项目、会话标题、agent 与工具；结果只回给原始
  会话的 permission Future。
- 全局执行 gate 最多允许三个会话同时实际 dispatch。超出的会话保持可取消的
  “等待资源”状态；每个会话内部仍由自己的 CommandBus 保证 FIFO。
- 当前会话保持热状态。非当前、无 pending command 且无权限等待的 runtime 在
  空闲十分钟后按 control → bus → orchestrator 顺序关闭；重开沿用既有
  session/load 与有界 bootstrap 契约。
- 草稿按 room_id 隔离，切换时保留文字、光标和图片引用；M4.7 不把未发送草稿
  跨进程持久化。

### 2.4 图片短引用

- 新粘贴图片按当前房间单调分配 `img-NNNN.png`，草稿和 timeline 使用
  `[图片 N]`。
- 解析短引用时只能在当前房间私有 `attachments/` 信任根下解析对应文件；仍执行
  普通文件、权限、PNG、大小、硬链接和目录逃逸检查。
- 历史 `[图片附件：绝对路径]` 继续只在同一信任根内兼容读取，不重写旧 timeline。

## 3. 不选择的方案

- **切换即取消任务**：把界面导航变成隐式破坏操作。
- **一个 Orchestrator 偷换多个 history**：原生 session、cursor、lease 和权限归属
  会交叉，违反单写者契约。
- **所有打开会话永久常驻**：多项目、多 agent 时资源增长无界。
- **模型标题或语义搜索**：为确定性本地导航引入不必要的成本、延迟与失败分支。
- **只在 UI 截短绝对图片路径**：timeline、恢复和会话摘要仍会保存噪声格式。

## 4. 验收

1. 旧默认/命名房间无需迁移即可出现在目录中；新标题不改变 room_id、CLI selector
   或 agent session 映射。
2. 当前/全部项目列表、元数据搜索、创建、切换、重命名和带标题确认的删除通过
   storage 与 Textual pilot 测试。
3. 两个以上会话可后台执行，事件、权限、草稿、时间线和未读状态不串房；第四个
   并发任务可见等待资源并可取消。
4. 后台完成/失败产生非阻塞通知；空闲超时只回收符合条件的后台 runtime，运行、
   权限等待和当前 runtime 不回收。
5. `[图片 N]` 原生图片 payload、旧格式兼容、路径逃逸与重复编号回归通过。
6. `bash scripts/check-harness.sh` 通过；另以两个临时会话做一次真实 agent 冒烟，
   不写目标项目文件，退出后无 owned 子进程残留。

## 5. 后果

TUI 从“一个进程只能看一个会话”升级为本机多会话工作台，同时保留每个房间的
单写者、权限与恢复边界。代价是进程内需要统一的会话目录、全局执行 gate、事件
归属和空闲回收；这些复杂度集中在 `SessionManager`，不进入具体 agent adapter。
