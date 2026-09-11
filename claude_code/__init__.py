"""Claude Code headless stream-json transport.

Claude Code 没有官方 ACP；官方编程化入口是 ``claude -p`` 的 stream-json
长连接模式。本包把该厂商协议封在深层模块里，对编排器暴露与其他 adapter
一致的 stateful session、权限、执行模式与 no-replay 契约。
"""
