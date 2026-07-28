"""Orchestrator：整个系统的大脑。

关键概念（学习要点）：
- **Hub-and-spoke（中心辐射）**：agent 之间从不直接对话。所有消息先进
  共享时间线（history），@谁就把上下文打包发给谁，回复再贴回时间线。
  这样不会有"两个 agent 互相@死循环"，也只有一个地方需要管上下文。
- **多种传输，两种上下文策略**（AgentSpec.transport）：
  - JSONL adapter：无头调用每次是全新会话，agent 看不到聊天记录，
    把最近 N 条对话记录塞进 prompt 一起发（transcript 快照）。
  - 有状态 adapter（`stateful_session`，ACP 或 app-server）：原生会话
    在 agent 侧保持，编排器
    只发**增量**——每个 agent 一个 history cursor，记录已交付到哪儿；
    每轮只发 cursor 之后的新消息（跳过 agent 自己的回复，那些本来就在
    它的 ACP session 里；首次 bootstrap 限发最近 N 条）。读 cursor →
    选增量 → stream → 推进 cursor 在每-agent delivery lock 内原子完成，
    同一 agent 的并发 dispatch 严格串行；cursor 只在成功交付后推进，
    失败不推进，下一轮补发增量，不丢上下文。
- **并发扇出（fan-out）**：一条消息 @多个 agent 时并行派发，互不等待。
- **Supervisor（中心协调者）**：注册表里的 host 是一个由 LLM 扮演的
  主持人。显式 @ 永远优先；用户没点名时，host 用一次调用直接回答或
  决定派发给谁（见 host.py）。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Callable

from acp.adapter import AcpKimiAdapter, AgentPermissionHandler
from adapters.base import (
    AgentAdapter,
    AgentDeliveryCancelledError,
    AgentDeliveryUncertainError,
    AgentEvent,
)
from adapters.opencode_adapter import OpenCodeAdapter
from codex_app_server.adapter import CodexAppServerAdapter
from host import MODERATOR_TEMPLATE, HostAgent
from storage.store import (MAX_READ_LIMIT, CorruptedStorageError, RoomLease,
                           RoomStore, WorkdirMismatchError, normalize_workdir)

HOST_NAME = "host"

_MENTION_RE = re.compile(r"@(\w+)")


@dataclass(frozen=True)
class AgentSpec:
    """一个工人 agent 的注册信息：名字 + 传输协议 + adapter 工厂。

    transport:
      - "acp"        ACP 有状态会话，编排器发增量上下文
      - "app-server" Codex 原生有状态 thread，编排器发增量上下文
      - "jsonl"      无头一次性调用，编排器发完整 transcript 快照

    新增 agent = 在这里加一行（写一个 adapter 类）。协议判断只看
    transport / adapter 能力声明，不散落 `if name == ...`。
    """

    name: str
    transport: str  # "acp" | "app-server" | "jsonl"
    factory: Callable[[], AgentAdapter]


# 工人 agent 注册表：ACP-first，JSONL 保留为 fallback。
# Kimi 是首个生产 ACP agent（命令 ["kimi", "acp"]）；
# Codex 使用官方 app-server 长连接；旧 Codex JSONL adapter 保留为 fallback。
AGENT_SPECS: tuple[AgentSpec, ...] = (
    AgentSpec("kimi", "acp", AcpKimiAdapter),
    AgentSpec("opencode", "jsonl", OpenCodeAdapter),
    AgentSpec("codex", "app-server", CodexAppServerAdapter),
)
AGENTS: dict[str, AgentSpec] = {spec.name: spec for spec in AGENT_SPECS}

# 发给 JSONL agent 的 prompt 模板：身份 + 对话记录 + 工作目录约定
_PROMPT_TEMPLATE = """\
你在一个名叫 myagents 的多 agent 聊天室里，身份是 "{name}"。
房间里有一个人类用户，可能还有其他 AI agent。
以下是最近的对话记录（格式 [发言者] 内容）：

{transcript}

请作为 {name}，针对用户最新的消息给出回复或执行其中的任务。
要求：直接输出内容，不要自我介绍，不要复述上面的记录。
如需读写文件、运行命令，都在当前目录内进行。
"""

# 发给 ACP agent 的 prompt 模板：session 原生记忆完整上下文，
# 只补"你上次被派发之后"的新消息（含其他 agent 的发言）
_ACP_PROMPT_TEMPLATE = """\
你在一个名叫 myagents 的多 agent 聊天室里，身份是 "{name}"。
房间里有一个人类用户，可能还有其他 AI agent。你的会话保持着完整上下文，
以下是自上次派发给你之后的新消息（格式 [发言者] 内容）：

{transcript}

请作为 {name}，针对最新的消息给出回复或执行其中的任务。
要求：直接输出内容，不要自我介绍，不要复述上面的记录。
如需读写文件、运行命令，都在当前目录内进行。
"""


@dataclass
class Message:
    speaker: str  # "user" 或 agent 名
    text: str
    # 持久化/内存序号：有 store 时来自 timeline 的单调 seq；无 store 时
    # 由 _append_message 单调分配。直接构造（兼容旧用法）时保持 0。
    seq: int = 0
    created_at: str = ""  # UTC ISO-8601；内存模式留空
    command_id: str | None = None


# on_event(agent_name, event) —— UI 层传进来的回调
EventCallback = Callable[[str, AgentEvent], None]


class _CheckpointError(Exception):
    """cursor/session_id checkpoint 落盘失败。

    必须穿透 _deliver_prepared 向 dispatch 传播：store 已不可信，不能把
    本轮伪装成普通 agent 失败（那会去 append timeline）。adapter 侧按
    stream_prepared 契约回收未提交的 fresh session。
    """


class _EventCallbackError(Exception):
    """区分 event sink 与 agent 异常；sink 失败不得被当成 agent 失败吞掉。"""

    def __init__(self, cause: Exception) -> None:
        super().__init__(str(cause))
        self.cause = cause


class OrchestratorClosedError(Exception):
    """Orchestrator 已关闭：拒绝新的 dispatch 和排队中的 delivery。"""


class Orchestrator:
    def __init__(self, workdir: str, history_limit: int = 12,
                 specs: tuple[AgentSpec, ...] = AGENT_SPECS, *,
                 store: RoomStore | None = None,
                 persistent: bool = True) -> None:
        self.workdir = workdir
        self.history_limit = history_limit
        self.specs = specs
        self.adapters: dict[str, AgentAdapter] = {
            spec.name: spec.factory() for spec in specs
        }
        # 主持人也注册进 adapters：@host 时和工人走同一条派发路径，
        # 只是 _build_prompt 会给它主持人角色的 prompt。
        # 主持人由 codex 扮演，用 read-only 沙箱：总结/仲裁/路由只需要看，不需要写。
        self.host = HostAgent(adapter=CodexAppServerAdapter(
                                  sandbox="read-only",
                                  reuse_thread=False,
                                  ephemeral_thread=True),
                              workers=[spec.name for spec in specs])
        self.adapters[HOST_NAME] = self.host
        # 持久化：persistent=True 默认按 workdir 打开 RoomStore；调用方也
        # 可注入自己的 store（必须属于同一 workdir，fail loudly 不静默换房）。
        # persistent=False 用于测试/一次性场景，不触碰任何磁盘状态。
        if not persistent and store is not None:
            raise ValueError("persistent=False 时不接受 store 参数")
        self.store: RoomStore | None = None
        if persistent:
            if store is None:
                store = RoomStore(workdir)
            elif store.workdir != normalize_workdir(workdir):
                raise WorkdirMismatchError(
                    f"store 属于 {store.workdir!r}，与 workdir {workdir!r} 不一致")
            self.store = store
        self.history: list[Message] = []
        # 无 store 时的内存 seq 分配起点；有 store 时 seq 来自 timeline。
        self._next_memory_seq = 1
        # 从 store 分页加载全部历史（read 单次上限 MAX_READ_LIMIT）。
        if self.store is not None:
            after_seq = 0
            while True:
                page = self.store.read(after_seq, limit=MAX_READ_LIMIT)
                for rec in page["items"]:
                    self.history.append(Message(
                        rec.speaker, rec.text, seq=rec.seq,
                        created_at=rec.created_at, command_id=rec.command_id))
                after_seq = page["next_after_seq"]
                if not page["has_more"]:
                    break
        # 每个有状态（ACP）agent 的 history cursor：已交付到 timeline 的哪个
        # seq（持久化序号，不是 list 下标）。只增不减；只在成功交付后推进。
        # 有 store 时从 state.json 恢复；cursor 超出当前 timeline 最大 seq
        # 说明状态与历史脱节，fail loudly——静默跳过会丢上下文。
        self._cursors: dict[str, int] = {}
        max_seq = self.history[-1].seq if self.history else 0
        for name, adapter in self.adapters.items():
            if not getattr(adapter, "stateful_session", False):
                continue
            cursor = 0
            if self.store is not None:
                cursor = self.store.get_agent_state(name)["cursor"]
                if cursor > max_seq:
                    raise CorruptedStorageError(
                        f"agent {name!r} 的持久化 cursor={cursor} 超出当前 "
                        f"timeline 最大 seq={max_seq}")
            self._cursors[name] = cursor
        # 单写者 lease：只读加载/校验全部通过后、返回给调用方前获取；
        # 之后本房间的所有写入都受 lease 保护。获取失败（RoomBusyError）
        # 时构造失败，此时尚无 lease 需要释放。
        self._lease: RoomLease | None = None
        if self.store is not None:
            self._lease = self.store.acquire_owner()
        # 每-agent delivery lock：读 cursor → 选增量 → stream 完整执行 →
        # 推进 cursor 是一个原子单元。没有它，两个针对同一 agent 的并发
        # dispatch 会都按旧 cursor 构造 prompt，重复/乱序投递。
        self._delivery_locks: dict[str, asyncio.Lock] = {}
        # 关闭标志：aclose() 原子置位；dispatch 与排队中的 _run_one 据此
        # 拒绝新工作，closed 后不再写 timeline、不再 start/load/prompt。
        self._closed = False

    def _append_message(self, speaker: str, text: str,
                        command_id: str | None = None) -> Message:
        """往 history 追加一条消息的唯一入口（含持久化）。

        有 store：先落盘（append 内部 flush+fsync），成功后才进 history；
        落盘失败抛异常，history 和 seq 都不动。无 store：分配单调内存 seq。
        """
        if self.store is not None:
            record = self.store.append(speaker, text, command_id=command_id)
            message = Message(speaker, text, seq=record.seq,
                              created_at=record.created_at,
                              command_id=record.command_id)
        else:
            message = Message(speaker, text, seq=self._next_memory_seq,
                              command_id=command_id)
            self._next_memory_seq += 1
        self.history.append(message)
        return message

    # ---- 注册信息 / 生命周期 ----

    def set_permission_handler(self, handler: AgentPermissionHandler | None) -> None:
        """把 TUI 的权限决策器注入所有支持它的 adapter（ACP）。
        handler 签名：async (agent_name, params) -> outcome。
        未注入时 ACP 一律 deny——这是安全契约，不是默认值偷懒。"""
        for adapter in self.adapters.values():
            setter = getattr(adapter, "set_permission_handler", None)
            if setter is not None:
                setter(handler)

    async def aclose(self) -> None:
        """统一关闭所有支持 aclose() 的 adapter（ACP 长驻进程在此回收）。
        adapter 内部用同一把锁保证 close 不与进行中的 prompt 竞态。

        幂等：先原子置 closed——之后的新 dispatch 直接拒绝，排队等
        delivery lock 的 _run_one 拿到锁后也会看到并放弃，绝不
        start/load/prompt agent。无论 adapter close 结果如何，
        最后都释放房间单写者 lease。"""
        self._closed = True
        try:
            await asyncio.gather(
                *(adapter.aclose() for adapter in self.adapters.values()
                  if hasattr(adapter, "aclose")),
                return_exceptions=True,  # 一个关不掉不耽误其他的回收
            )
        finally:
            if self._lease is not None:
                self._lease.release()
                self._lease = None

    # ---- 路由 ----

    def parse_mentions(self, text: str) -> list[str]:
        """从消息里提取 @agent，按出现顺序去重，忽略未注册的名字。"""
        seen: list[str] = []
        for name in _MENTION_RE.findall(text):
            if name in self.adapters and name not in seen:
                seen.append(name)
        return seen

    # ---- 上下文组装 ----

    def _delivery_lock(self, name: str) -> asyncio.Lock:
        lock = self._delivery_locks.get(name)
        if lock is None:
            lock = self._delivery_locks[name] = asyncio.Lock()
        return lock

    @staticmethod
    def _format(messages: list[Message]) -> str:
        if not messages:
            return "（暂无记录）"
        return "\n".join(f"[{m.speaker}] {m.text[:2000]}" for m in messages)

    def _messages_for(self, name: str) -> tuple[list[Message], int]:
        """有状态 agent 本轮的增量消息 + 交付目标 seq（快照末尾的 seq）。

        必须在 delivery lock 内调用：cursor 读取、增量选择、推进是一个
        原子单元，锁外读到的 cursor 可能已被并发轮次推进。

        cursor 是持久化的 timeline seq，不是 list 下标：
        - cursor>0：选 seq 严格大于 cursor 的新消息，跳过 agent 自己的
          回复（那些已经在它的 ACP session 里，重发就是重复上下文）。
        - cursor=0（bootstrap）：不发全部历史，只发最近 history_limit
          条，避免长聊天后第一次 @ 就无界发送。
        """
        snapshot = list(self.history)
        snapshot_end_seq = snapshot[-1].seq if snapshot else 0
        cursor = self._cursors.get(name, 0)
        if cursor > 0:
            msgs = [m for m in snapshot
                    if m.seq > cursor and m.speaker != name]
        else:
            msgs = [m for m in snapshot[-self.history_limit:]
                    if m.speaker != name]
        if not msgs:
            # 没有新消息也要给出触发点（快照窗口里可能只剩自己的发言）
            msgs = [m for m in snapshot if m.speaker != name][-1:]
        return msgs, snapshot_end_seq

    def _build_prompt(self, agent_name: str, messages: list[Message]) -> str:
        transcript = self._format(messages)
        if agent_name == HOST_NAME:
            template = MODERATOR_TEMPLATE
        elif getattr(self.adapters[agent_name], "stateful_session", False):
            template = _ACP_PROMPT_TEMPLATE
        else:
            template = _PROMPT_TEMPLATE
        return template.format(name=agent_name, transcript=transcript)

    # ---- 派发 ----

    async def dispatch(self, user_text: str, on_event: EventCallback,
                       command_id: str | None = None) -> None:
        """处理一条用户消息：记录 → 路由 → 并发派发 → 收回回复。

        路由规则：**显式 @ 永远优先**；用户没点名时交给 host 一次处理
        （它直接回答，或返回需要派发的 worker）。

        command_id：调用方（CommandBus）的命令 id；本轮 user 消息和所有
        agent 最终 timeline 记录（正常回复、失败占位、无文本占位）都带它，
        并随 committed 事件的 meta 一起下发。

        持久确认：用户消息落盘（或分配内存 seq）成功后才回调
        `("user", AgentEvent("committed", text,
        meta={"seq": ..., "command_id": ...}))`——
        UI 据此再显示用户文本，append 失败时什么都不显示。
        closed 后拒绝 dispatch，不写 timeline。
        """
        if self._closed:
            raise OrchestratorClosedError("Orchestrator 已关闭，拒绝 dispatch")
        committed = self._append_message("user", user_text,
                                         command_id=command_id)
        on_event("user", AgentEvent("committed", user_text,
                                    meta={"seq": committed.seq,
                                          "command_id": command_id}))
        # 快照（在第一个 await 之前）：等待 host 处理或 agent 启动期间，
        # 用户可能又提交了新消息进 history。JSONL agent 和 host 路由的
        # prompt 必须用快照拼接，否则并发消息会串话（agent 执行错任务）。
        # ACP agent 不走这个快照——它的增量起点（cursor）在 delivery lock
        # 内、拿到锁之后才读取，见 _run_one。
        snapshot = list(self.history)
        snapshot_text = self._format(snapshot[-self.history_limit:])
        targets = self.parse_mentions(user_text)
        if not targets:
            # decide 不在 _run_one 的异常保护里，自己兜底。契约：
            # 路由失败 → error 事件 + 确定性回退到第一个工人（可用性
            # 最高的路径）。绝不回退到 host adapter 本身——路由失败
            # 大概率就是它挂了，再调一次只会伪造出"无文本回复"。
            try:
                decision = await self.host.decide(
                    snapshot_text, self.workdir)
            except Exception as exc:
                on_event(HOST_NAME, AgentEvent("error", f"host 处理失败：{exc}"))
                targets = [self.specs[0].name]
                reason = f"路由失败，回退到 {targets[0]}"
            else:
                if decision.answer is not None:
                    # host 在“判断是否路由”的同一次 LLM 调用里已经给出最终
                    # 回答，直接落时间线；不再二次调用 host adapter。
                    on_event(HOST_NAME, AgentEvent(
                        "info", "host 直接回答（单次调用）"))
                    on_event(HOST_NAME, AgentEvent("text", decision.answer))
                    self._append_message(
                        HOST_NAME, decision.answer, command_id=command_id)
                    on_event(HOST_NAME, AgentEvent(
                        "done", meta={"hostAnswered": True}))
                    return
                targets, reason = decision.targets, decision.reason
            on_event(HOST_NAME, AgentEvent(
                "info", f"路由 → {'、'.join(targets)}（{reason}）"))
        await asyncio.gather(
            *(self._run_one(name, on_event, snapshot, command_id)
              for name in targets)
        )

    async def _run_one(self, name: str, on_event: EventCallback,
                       snapshot: list[Message],
                       command_id: str | None = None) -> None:
        adapter = self.adapters[name]
        if getattr(adapter, "stateful_session", False):
            # P1 契约：读 cursor → 选增量 → stream 完整执行 → 推进 cursor
            # 必须在该 agent 的 delivery lock 内完成，prompt 拿到锁之后才
            # 构造——否则并发的两个 dispatch 都按旧 cursor 构造 prompt，
            # 第二轮会重复/乱序投递。不同 agent 持不同的锁，并行扇出不受影响。
            async with self._delivery_lock(name):
                if self._closed:
                    # 用户消息已提交、但本轮排队期间 orchestrator 已关闭：
                    # 绝不 start/load/prompt agent
                    raise OrchestratorClosedError(
                        f"Orchestrator 已关闭，放弃对 {name!r} 的排队派发")
                if getattr(adapter, "stream_prepared", None) is not None:
                    # ACP restore 原语：prepare 与 prompt 同一锁生命周期，
                    # checkpoint（cursor/session_id）在 prompt 前原子提交。
                    failed, delivered_upto = await self._deliver_prepared(
                        name, adapter, on_event, command_id)
                else:
                    # 无 stream_prepared 的 stateful fake：纯内存 seq 路径
                    messages, delivered_upto = self._messages_for(name)
                    failed = await self._deliver(name, adapter, messages,
                                                 on_event, command_id)
                if failed is None:
                    # 成功交付后才推进 cursor，且只推进到本轮构造时的快照
                    # 末尾 seq：1) 失败不推进——下轮从旧 cursor 补发，不丢
                    # 上下文；2) 本轮进行期间到达的新消息没发出去，必须留给
                    # 下一轮；3) 自己的回复靠 speaker 过滤跳过。
                    # post-submit no-replay 例外已在 _deliver_prepared 的
                    # 专门异常分支里先持久化，不会走到这里。
                    # 有 store：先落盘（原子写），成功后才更新内存 cursor；
                    # 写失败向 dispatch 传播，内存/磁盘 cursor 都不动，
                    # 下一轮按旧 cursor 补发。
                    self._commit_cursor(name, delivered_upto)
        else:
            # 无状态（JSONL）agent：dispatch 瞬间的 transcript 快照
            await self._deliver(name, adapter,
                                snapshot[-self.history_limit:], on_event,
                                command_id)

    async def _deliver_prepared(self, name: str, adapter: AgentAdapter,
                                on_event: EventCallback,
                                command_id: str | None = None
                                ) -> tuple[str | None, int]:
        """ACP 路径：session prepare 与 prompt 在 adapter writer lock 内
        原子完成；返回（失败信息, 交付目标 seq）。

        make_prompt 是 checkpoint hook：在 prepare 判定之后、任何
        event/prompt 之前执行——
        a. fresh 且未 restored（load 失败/无 capability 回退 new）：新
           session 没有旧上下文，cursor 归 0（走 bootstrap 窗口）；
        b. load 成功（restored）或复用活跃 session（非 fresh）：保留
           当前 cursor，继续增量；
        c. 有 store：一次 set_agent_state 把 chosen cursor + 实际
           session_id 原子落盘，成功后才更新内存 cursor；写失败抛
           _CheckpointError，穿透本方法向 dispatch 传播——store 已
           不可信，绝不能伪装成普通 agent 失败去 append timeline。
        """
        resume_session_id = None
        if self.store is not None:
            resume_session_id = self.store.get_agent_state(name)["session_id"]
        delivered: dict[str, int] = {"upto": 0}

        def make_prompt(prep) -> str:
            if (prep.fresh and not prep.restored
                    and getattr(
                        adapter, "replay_history_on_fresh_session", True)):
                chosen = 0
            else:
                chosen = self._cursors.get(name, 0)
            if self.store is not None:
                try:
                    self.store.set_agent_state(
                        name, cursor=chosen, session_id=prep.session_id)
                except Exception as exc:
                    raise _CheckpointError(
                        f"agent {name!r} checkpoint 写失败：{exc}") from exc
            self._cursors[name] = chosen
            messages, upto = self._messages_for(name)
            delivered["upto"] = upto
            return self._build_prompt(name, messages)

        parts: list[str] = []
        failed: str | None = None
        done_meta: dict = {}
        try:
            async for ev in adapter.stream_prepared(
                    make_prompt, self.workdir, resume_session_id):
                if ev.kind == "delivery_committed":
                    # adapter 已确认用户 turn 被接受。必须先持久化，再公开
                    # 任何可能触发 events/UI 写入的后续事件。
                    self._commit_cursor(name, delivered["upto"])
                    continue
                if ev.kind == "done":
                    # done 不立即转发：等回复落盘成功后才发（见方法尾）
                    done_meta = ev.meta
                    continue
                self._emit_adapter_event(on_event, name, ev)
                if ev.kind == "text":
                    parts.append(ev.text)
        except _EventCallbackError as exc:
            raise exc.cause from exc
        except _CheckpointError as exc:
            # checkpoint 失败：发 error event 后原样抛出，不进 history
            on_event(name, AgentEvent("error", str(exc)))
            raise
        except AgentDeliveryUncertainError as exc:
            # turn/start 的应答丢失时服务端可能已开始执行。诚实记录失败，
            # 先持久 no-replay 边界，再做任何 callback/timeline 写入；否则
            # 后两者失败会留下旧 cursor，重启后可能重复工具副作用。
            self._commit_cursor(name, delivered["upto"])
            failed = str(exc)
            on_event(name, AgentEvent("error", failed))
        except AgentDeliveryCancelledError:
            # 保持 CancelledError 语义交给 CommandBus 标记 cancelled，但先
            # 固化 turn 已提交后的 no-replay 边界。
            self._commit_cursor(name, delivered["upto"])
            raise
        except Exception as exc:  # agent 崩溃不拖垮整个聊天室
            failed = str(exc)
            on_event(name, AgentEvent("error", failed))
        # history 必须诚实：有回复记回复，失败了记失败，
        # "无文本回复"只留给调用成功但真没说话的情况。
        reply = "".join(parts).strip()
        if reply:
            self._append_message(name, reply, command_id=command_id)
        elif failed is not None:
            self._append_message(name, f"（调用失败：{failed[:120]}）",
                                 command_id=command_id)
        else:
            self._append_message(name, "（无文本回复）", command_id=command_id)
        if failed is None:
            # 只有成功轮（append 也已成功，否则上面已抛出）才发 done
            on_event(name, AgentEvent("done", meta=done_meta))
        return failed, delivered["upto"]

    async def _deliver(self, name: str, adapter: AgentAdapter,
                       messages: list[Message],
                       on_event: EventCallback,
                       command_id: str | None = None) -> str | None:
        """执行一轮并把结果写回 history；返回失败信息（None = 成功）。"""
        prompt = self._build_prompt(name, messages)
        parts: list[str] = []
        failed: str | None = None
        done_meta: dict = {}
        try:
            async for ev in adapter.stream(prompt, self.workdir):
                if ev.kind == "done":
                    # done 不立即转发：等回复落盘成功后才发（见方法尾）
                    done_meta = ev.meta
                    continue
                self._emit_adapter_event(on_event, name, ev)
                if ev.kind == "text":
                    parts.append(ev.text)
        except _EventCallbackError as exc:
            raise exc.cause from exc
        except Exception as exc:  # agent 崩溃不拖垮整个聊天室
            failed = str(exc)
            on_event(name, AgentEvent("error", failed))
        # history 必须诚实：有回复记回复，失败了记失败，
        # "无文本回复"只留给调用成功但真没说话的情况。
        reply = "".join(parts).strip()
        if reply:
            self._append_message(name, reply, command_id=command_id)
        elif failed is not None:
            self._append_message(name, f"（调用失败：{failed[:120]}）",
                                 command_id=command_id)
        else:
            self._append_message(name, "（无文本回复）", command_id=command_id)
        if failed is None:
            # 只有成功轮（append 也已成功，否则上面已抛出）才发 done
            on_event(name, AgentEvent("done", meta=done_meta))
        return failed

    @staticmethod
    def _emit_adapter_event(
            on_event: EventCallback, name: str, event: AgentEvent) -> None:
        try:
            on_event(name, event)
        except Exception as exc:
            raise _EventCallbackError(exc) from exc

    def _commit_cursor(self, name: str, delivered_upto: int) -> None:
        """先落盘再更新内存；成功轮与 post-submit no-replay 共用。"""
        if self.store is not None:
            self.store.set_agent_state(name, cursor=delivered_upto)
        self._cursors[name] = max(
            self._cursors.get(name, 0), delivered_upto)
