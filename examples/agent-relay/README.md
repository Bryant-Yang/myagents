# Agent Relay

终端霓虹风的单人小游戏：旋转中央路由器，把彩色消息包路由到对应的 AI agent 节点。
独立于 `myagents` 核心代码，零依赖，原生 HTML/CSS/Canvas JavaScript。

## 运行

直接用浏览器打开 `index.html` 即可：

```bash
open examples/agent-relay/index.html
# 或启动本地静态服务
python3 -m http.server 8000 --directory examples/agent-relay
# 然后访问 http://localhost:8000
```

## 玩法

- `A` / `D` 或 `←` / `→`：旋转路由器指针
- `Space`：发送最接近中心的消息包 / 开始 / 重新开始
- 正确路由（指针对准包的目标 agent 扇区）：+100 × 连击分，降低过载
- 发错方向：连击清零，过载 +15
- 包超时撞到路由器：连击清零，过载 +20
- 过载满即宕机结束；单局 60 秒，包速与生成频率随时间递增
- 窗口缩放后画面自适应，可随时调整大小继续玩
