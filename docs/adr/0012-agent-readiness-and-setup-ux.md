# ADR-0012：Agent 就绪探测与缺失安装体验

- 状态：Accepted
- 日期：2026-08-12
- Owner：Bryant Yang
- 里程碑：M4.10

## 1. 背景

`AGENT_SPECS` 表示产品认识哪些 agent，不等于本机当前能启动哪些 agent。旧实现
在启动行和 `@` 候选中把所有注册项都写成“可用”，直到真正派发后才暴露
`executable not found`。这会清空用户草稿、产生失败 command，并让 host 把任务
路由给本机根本无法启动的 worker。

“命令未解析”也不能直接等价为“没有安装”：CLI 可能已经存在，只是当前 TUI
进程没有继承正确 PATH，或需要显式路径。产品必须报告观测事实，而不是猜测用户
机器状态。

## 2. 决策

### 2.1 注册与就绪分离

- 注册信息仍由 `AgentSpec` 提供；每个生产 spec 追加一个无副作用 readiness
  probe。协议和路径差异留在具体 probe/adapter，不进入通用路由分支。
- 通用 `AgentReadinessRegistry` 只暴露 `snapshot()`、`require()`、`refresh()`：
  TUI、Orchestrator、discussion 与 workflow 不自行解释 PATH 或厂商错误。
- 首版状态为 `ready`、`not_found`、`invalid`。文案使用“当前进程未检测到 CLI”，
  不把 PATH 不可见武断写成“未安装”。每个状态携带有界原因和人工 setup hint。
- production TUI 在启动/新建 runtime 时探测；测试和嵌入调用可以注入 fake probe
  或关闭系统探测，避免自动化依赖真实安装。

### 2.2 探测必须被动且只读

- probe 只允许读取环境变量、PATH、文件类型与可执行位；不得启动 CLI、联网、
  打开浏览器、读取登录会话、执行安装器或修改 shell 配置。
- 不自动执行 `brew`、`npm` 或其他安装命令，不删除、移动或替换现有安装。
- WorkBuddy 继续复用 R4 的 canonical executable 校验；App bundle 内私有 CLI
  或其他无效候选记为 `invalid`，不能为了显示 ready 而放宽安全策略。
- `/agents rescan` 重新读取当前进程环境并同步所有已加载 room；新发现的 adapter
  只为后续任务启用，不取消或重建正在运行的任务。之后新建的 room 复用同一组
  probe，不退回依赖本机状态的另一套默认配置。

### 2.3 派发资格与原子边界

- 显式点名的所有 agent 必须 ready；任何一个未就绪都在 timeline 写入前拒绝
  整条消息，不允许部分 fan-out 或静默换人。
- `/discuss` 在写入前检查所有参与者和 moderator；`/workflow` 检查 reviewer、
  implementer、verifier 以及固定 final host。
- 无 mention 消息先要求 host ready；host 路由候选只包含 ready worker。host
  不可用时进入手动点名模式，不回退到第一个注册 worker。
- session role 可以继续保存对暂时未就绪 agent 的定义；只有实际派发受阻，
  readiness 不删除角色、cursor、session id 或 timeline 事实。
- 外部 CommandBus/MCP 同样在 Orchestrator 内受资格门约束。TUI 额外在清空输入框
  前调用同一资格门，使本地用户保留原草稿；这不是第二套判断逻辑。

### 2.4 TUI

- 启动行显示 ready 数量、可用 agent 与待设置项；不再把注册表整体称为可用。
- `@` 候选保留全部已知 agent，ready 项优先；未就绪项置后并显示状态，保持
  可发现性但不伪装可派发。
- `/agents` 展示 worker 与 host 的状态、transport、原因和 setup hint；
  `/agents rescan` 只做被动重扫并显示结果。
- 未就绪、discussion/workflow 资格错误均显示可操作的本地错误，原输入文本和
  光标保持不变，不进入 timeline、不调用模型。

## 3. 不选择的方案

- **启动时执行每个 CLI 的 `--version` 或握手**：可能启动网络、认证、更新检查或
  浏览器，违反无副作用启动边界。
- **自动安装或修复 PATH**：包管理器、区域和 shell 初始化属于用户机器状态，
  当前产品没有足够权限模型与回滚能力。
- **隐藏未安装 agent**：用户无法发现产品能力，也得不到安装/路径指引。
- **缺失时自动换另一个 agent**：改变显式委托和 workflow 角色，违反路由事实。
- **仅在 adapter 抛 FileNotFoundError 后美化错误**：发生得太晚，草稿、command
  和 timeline 原子边界已经被破坏。

## 4. 验收

1. 零 CLI、部分 CLI 和全部 CLI 三种 fake 环境均能启动 TUI；测试不触碰本机安装。
2. `snapshot/require/refresh` 在 fake PATH 中正确区分 ready/not_found/invalid；
   rescan 后可从 not_found 变为 ready，无需重启。
3. 显式多目标、discussion 与 workflow 任一角色未就绪时整条拒绝，timeline、
   adapter 调用和 Git baseline 均不发生。
4. host 未就绪时无 mention 消息在 timeline 前拒绝；host 路由 prompt 不包含未
   就绪 worker。
5. TUI 候选 ready 优先、未就绪状态可见；提交未就绪目标后草稿与光标保留；
   `/agents` 与 `/agents rescan` 都是本地命令，不写 timeline、不调用模型。
6. probe 测试使用注入 resolver、临时普通文件和符号链接；不得安装、卸载、移动
   或启动真实 agent。完整 Harness 与 R1–R6 继续通过。

## 5. 后果

用户可以在缺少任意或全部可选 CLI 时正常打开聊天室，明确知道“注册能力”和
“当前机器就绪能力”的差别。代价是 `AgentSpec` 多一个 probe seam、TUI 多一个
状态视图；但 PATH、错误文案和资格判断集中在一个深模块，避免散落到每条路由。
