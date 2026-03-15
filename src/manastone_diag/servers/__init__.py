"""
Manastone MCP Servers

每个 server 对应一个独立的硬件子系统。
通过 config/servers.yaml 控制哪些 server 启动。
"""
from .base import AppState, init_shared_state, shutdown_shared_state, get_shared_state

__all__ = ["AppState", "init_shared_state", "shutdown_shared_state", "get_shared_state"]
