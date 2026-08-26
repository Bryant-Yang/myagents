"""DeepSeek Harness 的 myagents 专属 ACP transport。"""

from .adapter import AcpDshAdapter, dsh_readiness_probe

__all__ = ["AcpDshAdapter", "dsh_readiness_probe"]
