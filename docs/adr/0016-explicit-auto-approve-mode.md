# ADR-0016：显式 `/yolo` 会话级自动批准模式

- 状态：Accepted
- 日期：2026-08-26
- Owner：Bryant Yang
- 里程碑：M4.13

## 1. 背景

myagents 默认把 ACP、Pi RPC permission bridge 和 Codex app-server approval
统一交给 TUI 逐次决策。这适合处理不受信任务，但在用户明确信任当前
workspace，并希望让多个 agent 连续执行时，反复弹窗会中断流程。

这不能通过改成 adapter 默认自动放行来解决：脚本调用、新会话和无 UI 运行
都必须继续 fail-closed，workflow 的 review/verify/final 也必须保持 runtime
read-only 硬边界。

## 2. 决策

- 默认行为不变；只有用户在活动会话输入完整本地命令 `/yolo` 才开启，
  再次输入关闭。命令在持久化和路由前截获，不进入 timeline，也不调用 host。
- 该模式属于当前 myagents 进程中的当前 room；切换会话时按稳定 room id
  隔离，不写入 room state、用户配置或 agent session 长期授权。退出后
  所有 room 的临时状态自动失效。后台 runtime 的空闲回收不等于删除 room，
  重新激活该 room 时仍沿用本进程内状态；永久删除 room 时同步清理。
- 共享 TUI permission handler 只从当次 `params.options` 中选择非空的
  `kind=allow_once`；不伪造 `optionId`，不选 `allow_always`。请求未提供
  `allow_once` 时仍返回 `cancelled`。
- 自动模式不弹出 `PermissionScreen`，但窗口标题和固定任务区必须
  持续显示高风险状态；非 transport 镜像的权限结果仍进入 events/活动卡。
- adapter 和 client 构造器仍默认 `permission="deny"`，生产代码不显式构造
  `permission="auto"`。显式模式只是可替换的上层决策器。
- `read_only` 仍由 OpenCode/Qwen/CodeBuddy/DSH/Pi/Codex 各自 runtime/profile/
  adapter 硬拒绝写入和升权。Kimi/OpenCode JSONL fallback 仍是固定只读白名单。
- MCP/control 没有开启或关闭该模式的方法；它们提交到已启动 TUI 的任务
  会遵守目标 room 当前的进程内模式。

## 3. 安全边界

“自动批准”不等于 OS 级完全隔离或 sandbox bypass。被批准的 shell、网络、
进程或越界文件访问仍继承 myagents/agent 子进程的本机权限。因此该模式只
适用于用户明确信任的 workspace 和输入；不宣称可安全地无人值守处理不受信
仓库、外部内容或敏感凭据环境。需要该保证时必须另加 OS sandbox/容器。

## 4. 验收

1. `/yolo` 由本地命令注册表补全并在派发前截获；开启、关闭均不进入 timeline。
2. Textual + fake ACP 从真实派发入口触发 permission request，证明无弹窗、
   选择当次 `allow_once`、无挂起 Future，并在标题/固定任务区持续显示危险状态。
3. 两个 room 之间切换时模式不串会话；进程重启后默认逐次询问。
4. 现有 adapter 反例继续证明 `read_only` 不调用或不接受上层放行结果。
5. `scripts/check-redlines.sh` 继续证明默认 deny，生产构造器无
   `permission="auto"`。
