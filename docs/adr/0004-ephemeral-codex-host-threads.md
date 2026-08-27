# ADR-0004：Codex host 使用 ephemeral thread

- 状态：Superseded（[ADR-0017](0017-native-model-backed-host.md) 已改为
  room 级 HostBackend；Codex Host 使用独立 read-only adapter/session，本文的
  ephemeral 方案仅保留为历史决策）
- 日期：2026-07-28
- 里程碑：M4.1
- 作者：Bryant Yang
- 补充：[ADR-0003](0003-codex-app-server-transport.md)

## 1. 背景

ADR-0003 要求 Codex host 复用暖 app-server 进程、每轮建立干净 thread。host
prompt 自带有界 transcript 快照，复用原生 thread 会把旧快照与新快照重复叠加，
影响路由和直接回答。

但普通 `thread/start` 会把每次 host 判断落成 Codex 历史记录。Codex Remote
因此按聊天室目录展示多个标题相同的会话，标题内容还是内部 host 路由提示词。
这些记录不是用户主动创建的工作会话，也不具备恢复价值。

本机稳定版 app-server V2 schema 已提供 `ThreadStartParams.ephemeral`；对应 thread
不会 materialize 到磁盘。

## 2. 决策

- host adapter 保持 `reuse_thread=False`，每轮仍建立干净 thread。
- host 的 `thread/start` 增加 `ephemeral: true`，继续复用同一个暖
  app-server 进程，但不把路由 thread 写入 Codex 历史。
- host 在 app-server prepare 失败时使用的 `codex exec` fallback 同样增加
  `--ephemeral`，故障路径也不得持久化内部路由。
- `@codex` worker 保持非 ephemeral，并继续复用、持久化和恢复原生 thread。
- `ephemeral_thread=True` 只允许和 `reuse_thread=False` 组合；错误组合在
  adapter 构造时直接拒绝。
- 除 host 的 `ephemeral: true` 外，继续遵守 ADR-0003 的默认配置继承边界；
  不发送 model、effort、config、collaboration mode、plugin 或 MCP 覆盖。
- 已经落盘的旧 host thread 不自动删除或归档。历史清理由用户另行授权。

## 3. 验收

1. fake app-server 连续两轮收到两次 `thread/start`，参数均含
   `ephemeral: true` 和 `sandbox: read-only`。
2. 两轮 host turn 共用一个 app-server PID。
3. 生产注册中 host 为 ephemeral，Codex worker 为非 ephemeral。
4. host 的 JSONL fallback 命令包含 `codex exec --ephemeral`。
5. worker 既有“两轮同 PID/thread”和重启 `thread/resume` 测试保持通过。
6. 人工验收：连续发送多条无 mention 消息后，Codex Remote 不新增 host
   路由历史；显式 `@codex` 工作会话仍可见并可继续。

## 4. 后果

- Codex 历史只保留用户有意使用的工作 thread，不再被内部 host 路由污染。
- host 仍获得干净上下文和暖进程延迟优势。
- ephemeral thread 不可跨进程恢复；这符合 host 每轮自带完整快照、无需恢复的
  角色语义。
- app-server 协议升级后，需要继续用生成 schema 与真实 Codex Remote 人工验收
  确认 `ephemeral` 语义未漂移。
