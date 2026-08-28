# ADR-0018：能力受限的运行中插话与 Esc 取消

- 状态：Accepted
- 日期：2026-08-28
- 决策者：Bryant Yang

## 1. 背景

CommandBus 的普通输入是 FIFO。ADR-0009 的 `/steer` 只在 milestone workflow
阶段边界生效，并明确禁止向 ACP session 建立第二 writer。用户还需要两个直接、
可预测的键盘动作：`Esc` 取消当前 command，`Alt+↑` 尝试把 FIFO 中最早的排队
输入送入当前运行。

不同 transport 并不具备相同的运行中写入原语。Pi 官方 RPC 提供 `steer`，Codex
app-server 提供带 `expectedTurnId` 的 `turn/steer`；当前 ACP 与原生 model runtime
没有已验收的等价安全 seam。把插话伪装成取消后重投或普通 FIFO 输入会破坏
no-replay，也会误导用户。

## 2. 决策

### 2.1 产品语义

- 正常 composer 输入态按 `Esc` 精确取消当前 room 的 running command；原
  `Ctrl+X` 继续作为兼容快捷键。补全、活动导航和 modal screen 先处理自己的
  `Esc`；权限弹窗中的 `Esc` 取消整条 command，而不是只拒绝一次工具。
- 用户先按 Enter 创建 queued command；运行中按 `Alt+↑` 时只提升同 room FIFO
  中最早的一条。有多条时其余条目保持原顺序，composer 中尚未提交的草稿不参与、
  不清空。
- 插话复用被提升的 queued command，不创建额外 command；该输入不进入共享
  timeline、不推进普通 history cursor，也不改参与者、角色、权限、execution
  mode 或 `/yolo` 状态。

### 2.2 能力与目标选择

- workflow 优先使用 ADR-0009 已有阶段边界 steering，继续服从阶段、数量、字符、
  角色和权限限制；即使当前 stage adapter 是 Pi，也不得绕过 R6 改走 native steer。
- 非 workflow 只有当前 command 恰有一个活动 delivery，且该 adapter 显式提供
  `interject()` 时才接受。Pi 调用同一已 attested RPC 进程的官方 `steer`；Codex
  调用同一 app-server client 的官方 `turn/steer`，并携带原 `threadId` 与
  `expectedTurnId`。两者都不创建第二 prompt/turn/session/process/writer。
- 没有 queued command、零个活动 delivery、多个并发 delivery、目标已结束，或
  adapter 没有已验证 seam 时 fail-closed；被选输入仍留在原 FIFO 位置。ACP、
  原生 model Host 和普通 JSONL 当前均明确拒绝；agent Host 只投影其独立底层
  adapter 已声明的同轮 capability，因此 Codex Agent Host 可使用自己的
  `turn/steer`，不会借用 `@codex` worker。用户可选择 `Esc` 取消当前任务，或等待
  队列正常出队。Codex 若处于 review/manual compact 等不能接受 steer 的阶段，
  app-server 明确拒绝并同样保留队首。
- 通用层只检查 adapter capability，不按 agent 名或 transport 字符串分支。

### 2.3 持久化与 no-replay

- native 插话在协议写入前先追加 `interjection_requested` execution event，作为
  不重投边界；明确接受后追加 `interjection_accepted`，明确失败或结果不确定分别
  追加 `interjection_failed` / `interjection_uncertain`。
- 明确接受后，被提升的 queued command 直接 terminal，worker 以后取到其旧队列
  节点时跳过；其余 queued command 顺序不变。明确拒绝/写入前失败时源 command
  保持 queued；结果不确定时源 command 必须 terminal failed，禁止稍后作为普通
  command 再执行。
- Pi `steer` 或 Codex `turn/steer` 写入后丢失响应属于提交结果不确定，绝不自动
  重发。Codex response 还必须返回原 turn id；缺失或不匹配同样按 uncertain。
  只有官方 response 明确拒绝才按未接受报告。
- 单条插话最多 1000 字符。CommandBus 仍只有一个 command owner；adapter 的
  `interject()` 只能使用经证明允许并发控制写入的 transport seam。

## 3. 不选择的方案

- **取消后用新 command 重投**：原任务可能已产生副作用，既不是插话也违反
  no-replay。
- **直接发送 composer 草稿**：草稿尚未形成可见、可取消、可等待的 command，
  会绕开既有队列状态并让连续 Enter 的语义失真。
- **只调整 FIFO 顺序**：仍要等当前 command 完成，不是运行中插话。
- **对所有长连接统一再发 prompt**：ACP 等协议会形成第二 writer 或并发 prompt。
- **多 agent 时猜当前目标**：讨论/fan-out 可能同时运行，猜测会改变用户指定的
  协作边界。

## 4. 验收

1. Textual pilot 证明 Alt+↑ 选择最早 queued command、其余保持 FIFO、composer
   草稿不参与，并覆盖无队列/不支持能力的拒绝；Esc 补全优先和二次 Esc 取消。
2. 权限弹窗 Esc 同时收尾 permission Future 与 command。
3. CommandBus 证明队首选择、native intent 先于 transport acceptance 落盘、
   uncertain 源 command 不再执行；workflow 仍走 boundary steering。
4. Pi fake RPC 证明活动 prompt 接受 `steer` 而没有第二个 `prompt`；Codex fake
   app-server 证明 `turn/steer` 命中原 thread/turn、没有第二个 `turn/start`，并
   覆盖写后断线 uncertain；两类 adapter 都只在 durable delivery commit 之后开放
   interject。
5. HostBackend 回归证明 Codex-like agent Host 将底层 `interject()` 投影给同一
   活动 Host delivery；model Host 与没有该能力的 agent Host 仍保持 capability
   absent、队首不出队。
6. 相关测试、`git diff --check` 与 `bash scripts/check-harness.sh` 全部通过。
