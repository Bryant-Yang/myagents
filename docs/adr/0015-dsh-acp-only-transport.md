# ADR-0015：DeepSeek Harness ACP-only transport

- 状态：Accepted
- 日期：2026-08-26
- Owner：Bryant Yang
- 里程碑：M4.12

## 1. 背景与已核实事实

DeepSeek Harness（DSH）的通用 headless 入口是一次性纯文本进程：每次新建
session，没有 `session/load`、权限请求、图片、活动事件或可靠的 prompt 终局，
不能满足 myagents 的 stateful worker 契约。DSH SDK JSON-RPC 虽有 session/event，
但当前没有 prompt-scoped cancel、权限请求、session close、稳定协议版本或可用的
图片上传入口，同样不能作为生产 transport。

DSH 自带 ACP demo 是最接近现有 `AcpAdapter` 的参考实现，但 stock 版本缺少本产品
所需的 load/close、产品 identity、严格终局和工具权限证据，不能作为生产 transport。
stock DSH 已公开 AgentRegistry/AgentSetup、ToolRuntime、session 持久化/查询等组合
seam，足以让 myagents 插件自己拥有完整的 `dsh-myagents-acp` server 与 host，而
无需修改 DSH。本产品不直接复用宽工具集的 ACP demo。专用 server
必须支持标准 `session/load`、`session/close`、`session/cancel`、
`session/request_permission` 与 prompt 终局；图片仍按当次 initialize capability
动态决定。DSH 当前为 pre-release，协议/包布局升级没有兼容承诺，因此升级后必须
重新运行本 ADR 的 fake gate 与真实受限验收。

## 2. 决策

### 2.1 注册与启动入口

- 生产只注册
  `AgentSpec("dsh", "acp", AcpDshAdapter, dsh_readiness_probe)`；通用
  Orchestrator 不出现 DSH 名称分支。
- `AcpDshAdapter` 是 ACP-only，默认 `permission="deny"`，没有 headless、SDK
  JSON-RPC 或 JSONL fallback。prepare 失败与 prompt 提交后的任何失败都不换协议。
- 安装入口使用官方 `dsh` CLI：`MYAGENTS_DSH_CLI` 可显式指定单一可执行路径，否则
  从 PATH 被动解析。命令固定为 `<official-dsh> --profile myagents`，不解析 shell
  words，也不接受另行构建的 product-owned standalone ACP executable。
- 源码入口只接受 `MYAGENTS_DSH_SOURCE_ROOT`，并只用它定位已安装依赖、已构建的官方
  `<source-root>/apps/cli/lib/bin.js`；命令固定为
  `<absolute-node> <official-built-bin> --profile myagents`。它与源码仓库根执行
  `pnpm dsh --profile myagents` 是等价的官方 launcher 路径，但生产 adapter 直接使用
  built bin，避免 pnpm 改变 cwd。`MYAGENTS_DSH_SOURCE_ROOT` 只承担官方 launcher 定位；
  产品 host 组合由 profile 中的标准 bundle 加载。入口定位不依赖 ambient cwd；transport
  在 initialize 前把子进程 cwd 规范绑定为本轮目标 workspace，同一活跃进程不得跨
  workspace 复用。
- myagents 不运行 `pnpm`、build、安装器或 readiness 期的 CLI/真实 agent，也不硬编码
  本机用户目录。显式 `MYAGENTS_DSH_CLI` 无效时记为 `invalid`，不静默尝试另一入口。
- 两种 launcher 都只加载 stock `$DSH_HOME/profiles/myagents`。该 profile 的
  `package.json` 必须同时满足：`dependencies` 存在
  `@myagents/dsh-acp-host`；`dsh.profile.bundles` 精确、按序等于
  `["@deepseek-ai/dsh-base", "@myagents/dsh-acp-host"]`；解析后的 bundle package 精确为
  `@myagents/dsh-acp-host@<runtime-contract.json 的 hostVersion>`，并声明
  `dsh.bundle.patch=./cordis.patch.yml`。canonical package entry 与 patch 必须是普通、
  可读、位于解析后的 package root 内；entry 与 patch 的 SHA-256 必须分别匹配
  checked-in runtime contract，并以 no-follow、单链接、有界、读前后身份稳定的方式
  重验。profile 不完整
  或顺序/版本/文件身份漂移时 fail-closed；readiness 不替用户运行
  `dsh plugin add/install` 修复。安装是独立、显式的用户 setup 步骤：
  `dsh plugin --profile myagents add <myagents-bundle-path>`。
- `DSH_HOME` 先解析为 canonical profile home；其下 `profiles/` 与
  `profiles/myagents/` 必须是 profile home 内真实、非 symlink 的目录，profile
  `package.json` 必须以 no-follow、单链接、有界、读前后身份稳定的方式读取。stock DSH
  会在 bundle 之后继续应用 `$DSH_HOME/cordis.patch.yml` 与
  `$DSH_HOME/profiles/myagents/cordis.patch.yml`，因此两个 later-wins user patch
  都只能缺失，或是 canonical、单链接、不超过 64 KiB 且忽略空行/注释后唯一语义行
  精确为 `[]` 的普通文件。空文件、仅注释文件、任何有效 patch、symlink、hardlink、
  越界或读取中身份变化全部 fail-closed；spawn 前以及复用活跃进程的下一轮前均重验。
- 子进程状态根显式设为绝对路径 `DSH_ACP_PERSISTENCE_DIR`：尊重用户已有值，
  否则使用 `${XDG_STATE_HOME:-~/.local/state}/myagents/dsh-acp`。根目录下固定派生
  `sessions/`、`runtime-home/`、`attachment-home/` 与 `runtime-home/agents/`；
  `DSH_HOME` 必须保留为包含 `profiles/myagents` 的 stock profile home，
  `DSH_AGENTS_HOME` 精确绑定 `runtime-home/agents/`。产品状态根不得与 profile home、
  不可变 DSH checkout 或目标 workspace 任一方向重叠。probe 与 adapter 构造只解析
  路径，不创建目录。profile home 中已有的 `settings.yaml` 与
  `.credentials.yaml` 只作为 no-follow、单链接、1 MiB 内的只读输入，内容原子复制到
  `config-inputs/` 的 mode-0600 产品文件后再以 `watch=false` 挂载；host 不接触原文件。
  配置、派生状态、canonical plugin/CLI 与 workspace/source 不得重叠或经 symlink 逃逸。
- 产品专属 Python adapter/readiness、TypeScript ACP server 与标准 bundle 的唯一
  事实源是 myagents `dsh_acp/`。bundle 的 canonical `package.json`、entry 与
  `cordis.patch.yml` 只能位于 `dsh_acp/plugin`。DSH checkout 是不可变依赖：不得复制
  `@myagents/dsh-acp-host` 源码，不得修改 core、官方 ACP 包、示例或测试，也不得从
  未导出的 package `src/*` 深层导入。只允许通过 DSH 已公开的 agent/AgentSetup/
  ToolRuntime/持久化 API 组合能力。官方 launcher 与标准 profile/bundle 是唯一正式
  启动形态；旧 custom `tsx/src/bin.ts` 与 standalone ACP CLI 均不再是产品契约。
- readiness 被动读取官方 CLI identity、profile manifest、依赖解析结果与 bundle
  name/version/entry/patch；源码模式额外验证官方 built CLI 属于所选 checkout。它不再
  审计整个 DSH 源码树，也不解析执行模块。产品 ACP
  identity、permission、load/close 与 terminal 行为仍由 initialize/fake/真实 gate 证明，
  不能用“文件存在”替代。

- 产品持久化固定 `compression=none`、`packChunks=false`。`session/load` 先通过 stock
  public `SessionPersistence.list/locate` 得到单一 artifact，再以 `O_NOFOLLOW`、canonical
  containment、逐行 seq 与 stat identity 检查限制 4096 events / 16 MiB；只有扫描、
  materialize 与 resume 前后的 identity 均一致才恢复。public seam 不能证明时不广告 load。

### 2.2 readiness 与 initialize hard gate

`dsh_readiness_probe` 只读取环境、PATH、真实路径、文件类型、可读/可执行位、profile
与解析后 bundle manifest：未发现官方 installed CLI 且未配置 source root 为
`not_found`；显式 CLI/source built launcher 无效，或 profile/dependency/exact bundles/
plugin name/version/entry/patch、profile canonical ownership，或任一 later-wins user
patch 任一不满足为 `invalid`；完整入口才为 `ready`。probe
不调用 `dsh plugin list/why` 或 `--dump-config`，因为 stock 实现会初始化或协调 profile，
并非严格被动；也不执行握手、登录、模型调用或安全探针。自动化只使用临时
`DSH_HOME` 构造 profile 正反例，不读取或修改用户安装。

第一次真实连接还必须在任何 `session/new` / `session/load` / prompt 前确认：

- `agentCapabilities.loadSession === true`；
- `agentCapabilities.sessionCapabilities.close` 是对象。
- `agentInfo.name === "dsh-myagents-acp"` 且 `version ===` checked-in
  `runtime-contract.json` 的 `hostVersion`；
- `agentInfo._meta` 精确满足下述版本化 wire schema。

`_meta` 的五个必需字面量 key 及类型/值契约为：

| literal key | JSON type | 允许的精确值 |
| --- | --- | --- |
| `deepseek.ai/dsh-myagents-profile` | string | `"workspace-write"` 或 `"read-only"`，且必须与当前进程的 `DSH_ACP_PROFILE` 一致 |
| `deepseek.ai/dsh-myagents-policy-revision` | integer | checked-in `runtime-contract.json` 的 `policyRevision` |
| `deepseek.ai/dsh-myagents-read-only-tools` | array of strings | 按顺序精确为 `["read", "glob", "grep"]` |
| `deepseek.ai/dsh-runtime-version` | string | checked-in `runtime-contract.json` 的 `dshRoot.version` |
| `deepseek.ai/dsh-compatibility-revision` | integer | checked-in `runtime-contract.json` 的 `compatibilityRevision` |

- `deepseek.ai/dsh-myagents-profile` 必须是 JSON string，值精确等于当前
  `DSH_ACP_PROFILE` 的 `workspace-write` 或 `read-only`；
- `deepseek.ai/dsh-myagents-policy-revision` 必须是 JSON integer，值精确等于
  checked-in contract 的 `policyRevision`，boolean `true` 不得借数值相等通过；
- `deepseek.ai/dsh-myagents-read-only-tools` 必须是 JSON array，且按顺序
  精确为三个 string `read`、`glob`、`grep`；缺项、重复、换序或
  额外工具都失败。
- runtime version 与 checked-in DSH compatibility contract 必须精确一致；compatibility
  revision 必须是 JSON integer，值精确等于 contract 的 `compatibilityRevision`，
  boolean `true` 不得借数值相等通过。

**版本身份的单一事实源与续约**：DSH runtime 版本、bundle 版本、两个 revision 与
全部 entry SHA-256 只存于 checked-in
`dsh_acp/plugin/runtime-contract.json`（经 `dsh_acp/contract.py` 加载校验）。
Python adapter、TypeScript plugin（build/vitest 经 esbuild define 注入）、测试与
本文档一律引用契约值，不得复制字面版本号。升级 DSH checkout 后运行
`scripts/dsh-contract-refresh.py`（默认 dry-run 打印 diff，`--accept` 落盘并同步
plugin `package.json` peerDependencies，最后用
`check-dsh-runtime-contract.py` 同一算法自证）。`compatibilityRevision` 与
`policyRevision` 只能人工随语义变更 bump，refresh 不改写。版本/哈希漂移在启动前
fail-closed 的要求不变。

host 可以携带其他 `_meta` 扩展，但不能替代或改写上述五项。

任一缺失都关闭进程并 block，不能退化成不可恢复 session。图片 capability 是可选项；
只有 `promptCapabilities.image === true` 时，通用 ACP client 才从房间附件信任根发送
inline PNG block，缺少图片能力不会阻断纯文本任务。上述 identity/policy metadata 是
专用 host 的版本化 wire contract；未来改变 profile 语义或工具闭集必须提升 policy
revision，并同步升级 adapter、ADR、fake server 与真实验收，不能只保持名称相同。

### 2.3 execution profile 与权限

`DEFAULT` 与 `WORKSPACE_WRITE` 都启动 stock `--profile myagents` 并设置
`DSH_ACP_PROFILE=workspace-write`；workflow 的 `READ_ONLY` 启动同一 stock profile 并
设置 `DSH_ACP_PROFILE=read-only`。跨越这两个 execution safety profile 时必须关闭旧
session/进程、禁止 load 旧 session，并建立新进程、新 session；切回普通轮同样重建。

专用 DSH host 必须把安全边界放在 runtime，而不是 prompt：

- `read-only` 只允许经过守卫的 `read` / `glob` / `grep` 闭集；write、shell、
  network、process、subagent、MCP、scoped tool、`run_code` 和同名 shadow tool 必须
  在执行前 hard-deny；
- `workspace-write` 的安全读取可直接执行，其他工具在执行前发标准
  `session/request_permission`，只提供当次 `allow_once` / `reject_once`；
- adapter 在每次请求上重新检查 options：必须恰有两个不同的非空 `optionId`，kind
  闭集严格等于 `allow_once` / `reject_once`；`allow_always`、重复 id、缺项或新增项
  一律在进入 TUI 决策前 cancelled；
- 无 handler、handler 异常、空/畸形 outcome 或不属于本次 `params.options` 的
  `optionId` 一律 `cancelled`。

myagents 的 permission handler 只能证明 ACP request/response 绑定，不能替代 DSH
内部工具 guard，也不宣称提供 OS sandbox。DSH host 的插件顺序、工具来源闭集或
read-only guard 缺少独立证据时，相关 profile 必须 block，不能因为 CLI 可启动就
宣称安全。

### 2.4 session、no-replay 与生命周期

- adapter 是 DSH session 的唯一 writer。重启优先 `session/load` 恢复已持久化
  session；仅标准 `-32002` / `-32601` 的确定拒绝可按通用
  fresh-session 契约归零 cursor 并有界 bootstrap。generic `-32000`、
  policy/quota/backend 或 transport 错误均 fail-closed，不新建 session 绕过。
- DSH load 可能重放大量历史通知。无认证 method 的 prepare 阶段直接丢弃这些
  通知，不积压也不作为本轮输出；有认证 method 时只使用 64 条有界队列，溢出即
  fail-closed 并回收连接。专用 host 固定使用 uncompressed/unpacked JSONL，并在
  `readSession` materialize 前通过 public `list()` / `locate()` 对目标文件执行
  no-follow 扫描：最多 4096 个事件、16 MiB；扫描、materialize 和 resume 前后均复核
  文件身份，超限或变化立即回收 owner。公共持久化 seam 不能证明该边界时不广告或
  拒绝 `session/load`。
- prompt 进入 ACP 后，首个 update/权限活动或最终响应先形成
  `delivery_committed`，Orchestrator 持久化 no-replay cursor 后才公开事件。断线、
  静默超时、取消未确认或结果不确定都不自动重发，也不换协议。
- 只有标准 `stopReason=end_turn` 产生 `done`；`max_tokens`、
  `max_turn_requests`、`refusal`、`cancelled`、缺失与未知值都在 cursor 已提交后
  抛出确定性 `AcpError`，不得伪装成功。
- 外层 task 取消、`stream.aclose()`、inactivity timeout 以及其他任何
  实际主动发送 `session/cancel` 的路径，都必须持锁有界等待原
  prompt 的精确
  `stopReason=cancelled` 终局；首个 update 前的已发送取消同样形成 no-replay cursor，
  `end_turn`、空值、未知值或未确认都会关闭连接。只有显式 pre-send 失败保留旧 cursor。
  profile reset 与 `aclose()` 对广告 close capability 的活跃 session
  先有界发送 `session/close`，随后按进程组 SIGTERM → SIGKILL 兜底回收。
- DSH committed assistant text/tool updates进入通用 ACP 事件模型；reasoning 正文
  不展示。没有 capability 或真实 provider/model 证据时，不宣称图片、token stream、
  reasoning、steer、subagent 可见性或长期 compaction 已获支持。

## 3. 明确 block 的情况

- 未找到官方 installed CLI 且未配置 source root，或显式 CLI/source built bin 不满足
  被动检查；
- stock `profiles/myagents` 不存在，dependency 缺失，bundle 顺序不是精确 base → host，
  或解析后的 package name/version/entry/`dsh.bundle.patch`/patch 文件漂移；
- `profiles/`、`profiles/myagents/` 或 profile manifest 经 symlink/hardlink/越界替换，
  或 home/profile 任一 later-wins `cordis.patch.yml` 不是缺失或精确语义 `[]`；
- profile home/source/state/workspace/config 拓扑重叠或 symlink 逃逸；
- initialize 缺 `loadSession` 或 `sessionCapabilities.close`；
- DSH 专用 host 的 read-only 工具闭集、同名 shadow/scoped/run-code guard 未获证；
- workspace-write 风险工具不能在执行前产生绑定本次 options 的 permission request；
- profile 切换复用旧进程/session，或 load 的 cwd/session 绑定无法验证；
- cancel/close、post-submit no-replay、进程组回收没有 fake 证据；
- 把 headless、SDK JSON-RPC 或其他协议作为 prepare/post-submit fallback；
- DSH、官方 CLI、profile 或 bundle 版本/布局变化后未重跑本 ADR gate。
- 专用 ACP server 需要修改 DSH checkout、依赖 stock ACP demo 的产品外行为，或必须
  从未公开的 `src/*` 深层导入才能成立。

这些 block 发生在具体 probe/adapter/DSH host，不在通用 Orchestrator 添加按名逻辑。

发布构建还必须先以 source-only preflight 把实际 DSH Git HEAD、官方 built CLI、
root/package versions、ACP SDK public entry、`publicPackages` 的 `src/index.ts` 与
实际 `exports` runtime entry，以及当前平台的 esbuild entry/binary SHA-256 逐项对照
checked-in `runtime-contract.json`；只有通过后才能执行该已验证 build tool。构建完成后
再做 built bundle entry/patch postflight。该核验只属于显式 release/package gate，
不把 source-tree fingerprint 耦合回运行时 adapter。

## 4. 自动化验收

默认 Harness 只调用本地 fake ACP server：

1. 注册固定为 `dsh/acp/AcpDshAdapter`、默认 deny、无 fallback；
2. readiness 通过临时 `DSH_HOME` 覆盖未配置、无 node、installed CLI/source built bin
   缺失、profile 缺失、dependency/bundle 顺序/name/version/entry/patch 漂移、
   profile parent/profile/manifest/patch symlink、有效 home/profile user patch、
   空或仅注释 patch、plugin/config/CLI 无效和 ready，且候选脚本不会被执行；
3. installed 与 source official launcher 的 argv 都使用绝对路径和固定
   `--profile myagents`；状态根及固定子树均为 canonical 绝对路径且 probe 不创建；
   profile home/source/state/workspace/config 重叠在 spawn 前失败；进程 cwd 规范绑定目标
   workspace，活跃 session 跨 cwd fail-closed；`DSH_ACP_PROFILE` 的
   workspace-write ↔ read-only 往返产生新 PID、新 session，禁止跨 execution profile
   load；readiness 后新增 later-wins patch 会在 spawn 或下一轮 prompt 前触发重验并
   回收连接；
4. initialize 缺 load/close capability 时在 new/load/prompt 前失败；
5. 默认、畸形 handler、`allow_always`、重复 option id 与 read-only 注入 allow 都
   fail-closed；合法且唯一的 `allow_once` / `reject_once` 可逐次决策；
6. load 成功恢复 opaque session，历史通知 flood 不建立 prepare queue、不外泄；
   durable replay 超过 4096 事件或 16 MiB 时在 resume/publication 前回收且零 update；
7. post-submit 断线只出现一次 prompt 且记为 uncertain/no-replay；prepare 失败零 fallback；
8. 首个 update 前取消仍提交 no-replay，且 cancel 只接受精确 `cancelled` 确认；
   session/close 与进程退出均完成；仅 `end_turn` 成功，其他标准、空或未知 terminal
   在 commit 后失败；无残留 fake server；
9. image 广告时发送可信 PNG block，未广告时同一图片任务在 prompt 前 block；
10. R1/R2/R4 静态红线锁定 deny、AgentSpec-only、官方 launcher、stock profile/标准
    bundle、capability/no-fallback。

证据入口为 `tests/test_dsh_acp.py`、`tests/fake_acp_server.py`、
`tests/test_phase2.py` 与 `scripts/check-redlines.sh`。标准 bundle/ACP host contract 归
`dsh_acp/plugin/tests/` 所有；它不进入无 DSH 依赖的默认 Harness。release 验收必须
使用临时 `DSH_HOME` 安装/构造 `myagents` profile，要求 stock checkout 起始完全干净，
并运行 bundle、权限、load/close 与终局测试；命令前后该 checkout 的 HEAD、
tracked/untracked 状态和包含 ignored 文件内容 SHA-256 的完整 `lstat` 文件树必须完全
一致：

```bash
MYAGENTS_DSH_SOURCE_ROOT=/absolute/path/to/deepseek-harness \
  bash scripts/check-dsh-plugin.sh
```

## 5. 真实验收清单

真实 DSH 不进入默认 gate。发布前需在受限临时 workspace 与临时 `DSH_HOME` 中逐项
记录 DSH commit、Node/CLI 版本、profile manifest/bundle version、session id、PID、
工作区 diff 与残留进程：

1. installed 官方 `dsh` 与 source 已构建官方 CLI 两条入口都以
   `--profile myagents` 完成 initialize/new/prompt/close；
2. plugin add 后 profile 的 dependency、exact base → host bundle 顺序、解析后的
   `@myagents/dsh-acp-host@<contract hostVersion>`、entry 与 patch 精确；缺失、换序、版本漂移、普通
   dependency 冒充 bundle、profile parent/manifest symlink escape，以及 home/profile
   later-wins 有效 patch 均被动阻断；
3. 重启后 `session/load` 恢复同一 session，cwd 不匹配和未知 id 明确失败，历史输出
   不重放到当前回复；
4. workspace-write 中安全读无需审批；write/shell/network/subagent 等风险工具先弹
   `allow_once/reject_once`，deny 零副作用、allow 精确一次且下次重新询问；
5. read-only 中 read/glob/grep 正常，write/shell/network/process/subagent/MCP/
   scoped/run-code/shadow 全部在执行前阻断，工作区与 Git 指纹不漂移；
6. execution safety profile 双向切换 PID/session 都变化，旧 session 不 load；
7. prompt 提交后断线/超时不重放，取消有明确 `cancelled`，close 后无 DSH 或 descendant
   残留；
8. 只有在 initialize 广告 image 且真实 provider/model 明确支持时才验证可信 PNG；
   未广告时纯文本仍正常且图片任务在发送前 block；
9. 长 session、compaction、max-token/refusal terminal、工具事件与输出压力需单独记录。
10. 验收前后 DSH checkout 的 HEAD 与 `git status --short` 必须一致且为空；任何测试或
   启动过程写入 DSH 仓库都视为失败。

fake 通过只表示 myagents adapter 合同成立；真实能力只能按下面已执行证据声明。

2026-08-26 的标准路径验收使用临时 `DSH_HOME` 与临时 workspace，未写用户 profile：

- 官方 `plugin --profile myagents add` 安装打包产物后，source launcher 由两个独立进程
  完成 `new → prompt → close → load → prompt → close`；session
  `7afca581-e99c-4a2f-8631-4efaec794fd8` 第二轮 `restored=true`，两轮精确返回
  `DSH_FINAL_PROFILE_OK`、`DSH_FINAL_PROFILE_RESUME_OK`；
- 同一官方可执行文件通过显式 `MYAGENTS_DSH_CLI` 走 installed-style resolver，真实
  workspace-write 轮产生且只产生一次 `allow_once/reject_once` 请求；reject 后目标文件
  不存在。切换 read-only 后 fresh session id 改变，风险操作未进入权限 UI且目标文件
  仍不存在；
- release gate 通过 12 个测试文件、151 项 host contract、Oxlint、严格 TypeScript、
  可复现构建、官方 dump-config 与连续 vision 握手；前后 DSH HEAD、Git 状态和包含
  ignored 内容哈希的完整文件树一致，原 settings/credentials 全属性及哈希不变，退出
  无残留进程。

旧 custom `tsx` source host 的探针继续 **superseded**。本机没有为了验收安装全局 DSH，
因此独立发布包形态的 installed CLI、真实主动 cancel 时延、长 session/compaction、
压力终局与真实图片模型仍属于人工边界，不得由上述证据外推。

## 6. 不选择的方案

- **headless one-shot fallback**：没有 stateful restore、权限和可靠取消，并会制造
  跨协议重放窗口。
- **SDK JSON-RPC runtime**：当前缺 prompt-scoped cancel、permission、close 和稳定
  版本协商，不能满足 worker lifecycle。
- **复用通用 DSH ACP demo**：宽工具集与原 profile 不能证明 workflow read-only。
- **绕过 stock profile 启动 custom `tsx/src/bin.ts` 或 standalone ACP CLI**：会复制官方
  launcher/profile 职责，要求另一套源码解析与环境注入契约；统一改用标准 bundle +
  stock `dsh --profile myagents`。
- **修改 DSH core/官方 ACP/示例**：会把产品契约绑到私有补丁，破坏升级与所有权边界；
  stock public seam 不足时应阻断能力，而不是给依赖打补丁。
- **在 Orchestrator 按 `dsh` 分支**：污染通用路由层，也绕开 `AgentSpec`/adapter
  的现有能力抽象。
- **readiness 启动 `--help`、build 或模型 probe**：违反被动探测和零副作用启动边界。
