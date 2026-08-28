# ADR-0018：能力受限的运行中插话与 Esc 取消

- 状态：Accepted
- 日期：2026-08-28
- 决策者：Bryant Yang

## 1. 背景

CommandBus 的普通输入是 FIFO。ADR-0009 的 `/steer` 只在 milestone workflow
阶段边界生效，并明确禁止向 ACP session 建立第二 writer。用户还需要两个直接、
可预测的键盘动作：`Esc` 取消当前 command，`Alt+↑` 尝试把草稿送入当前运行。

不同 transport 并不具备相同的运行中写入原语。Pi 官方 RPC 提供 `steer`；当前
ACP、Codex app-server 与原生 model runtime 没有已验收的等价安全 seam。把插话
伪装成取消后重投或普通 FIFO 输入会破坏 no-replay，也会误导用户。

## 2. 决策

### 2.1 产品语义

- 正常 composer 输入态按 `Esc` 精确取消当前 room 的 running command；原
  `Ctrl+X` 继续作为兼容快捷键。补全、活动导航和 modal screen 先处理自己的
  `Esc`；权限弹窗中的 `Esc` 取消整条 command，而不是只拒绝一次工具。
- composer 中有非空草稿时按 `Alt+↑` 发起插话。成功后清空草稿；拒绝、失败或
  不确定时保留草稿，并显示可操作错误。
- 插话不是新 command，不进入共享 timeline、不推进普通 history cursor，也不
  改参与者、角色、权限、execution mode 或 `/yolo` 状态。

### 2.2 能力与目标选择

- workflow 优先使用 ADR-0009 已有阶段边界 steering，继续服从阶段、数量、字符、
  角色和权限限制；即使当前 stage adapter 是 Pi，也不得绕过 R6 改走 native steer。
- 非 workflow 只有当前 command 恰有一个活动 delivery，且该 adapter 显式提供
  `interject()` 时才接受。当前首个实现是 Pi：调用同一已 attested RPC 进程的官方
  `steer`，不创建第二 prompt/session/process。
- 零个活动 delivery、多个并发 delivery、目标已结束，或 adapter 没有已验证 seam
  时 fail-closed。ACP、Codex app-server、原生 Host 和普通 JSONL 当前均明确拒绝；
  用户可选择 `Esc` 取消或 `Enter` 排队。
- 通用层只检查 adapter capability，不按 agent 名或 transport 字符串分支。

### 2.3 持久化与 no-replay

- native 插话在协议写入前先追加 `interjection_requested` execution event，作为
  不重投边界；明确接受后追加 `interjection_accepted`，明确失败或结果不确定分别
  追加 `interjection_failed` / `interjection_uncertain`。
- Pi `steer` 写入后丢失响应属于提交结果不确定，绝不自动重发。只有官方 response
  明确拒绝才按未接受报告。
- 单条插话最多 1000 字符。CommandBus 仍只有一个 command owner；adapter 的
  `interject()` 只能使用经证明允许并发控制写入的 transport seam。

## 3. 不选择的方案

- **取消后用新 command 重投**：原任务可能已产生副作用，既不是插话也违反
  no-replay。
- **把草稿提升到 FIFO 队首**：只能在当前 command 完成后执行，用户会误以为已
  影响当前运行。
- **对所有长连接统一再发 prompt**：ACP 等协议会形成第二 writer 或并发 prompt。
- **多 agent 时猜当前目标**：讨论/fan-out 可能同时运行，猜测会改变用户指定的
  协作边界。

## 4. 验收

1. Textual pilot 证明 Alt+↑ 成功清空、拒绝保留、Esc 补全优先和二次 Esc 取消。
2. 权限弹窗 Esc 同时收尾 permission Future 与 command。
3. CommandBus 证明 native intent 先于 transport acceptance 落盘；workflow 仍走
   boundary steering。
4. Pi fake RPC 证明活动 prompt 接受 `steer` 而没有第二个 `prompt`；adapter 只在
   durable delivery commit 之后开放 interject。
5. 相关测试、`git diff --check` 与 `bash scripts/check-harness.sh` 全部通过。
