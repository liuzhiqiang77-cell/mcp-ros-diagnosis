"""
manastone-motion
运动控制器状态 MCP Server（M2 功能，当前为 stub）

工具：
  motion_status   - 运动控制器状态（步态、sport mode、里程计）
  motion_alerts   - 当前活跃运动告警
"""
import json
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from mcp.server.fastmcp import FastMCP, Context

from .base import AppState, init_shared_state, shutdown_shared_state, get_shared_state

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(server: FastMCP, **kwargs) -> AsyncIterator[AppState]:
    state = await init_shared_state(**kwargs)
    logger.info("manastone-motion ready (stub)")
    try:
        yield state
    finally:
        await shutdown_shared_state()


def create_server(**init_kwargs) -> FastMCP:
    from functools import partial
    mcp = FastMCP(
        "manastone-motion",
        lifespan=partial(_lifespan, **init_kwargs),
    )

    @mcp.tool()
    async def motion_status(ctx: Context = None) -> str:
        """
        获取运动控制器状态（步态模式、sport mode、里程计健康）。
        当前为 M2 stub，返回基础话题数据。
        """
        s = get_shared_state()
        motion_topics = [
            t for t in s.schema.topics
            if t.component_group == "motion_controller"
        ]
        if not motion_topics:
            return json.dumps({
                "status": "not_configured",
                "message": "未配置运动控制话题（SportModeState 等）",
                "m2_note": "完整运动状态监控功能将在 M2 实现",
            }, ensure_ascii=False, indent=2)

        results = []
        for t in motion_topics:
            raw = await s.dds_bridge.get_topic_data(t.topic)
            results.append({
                "topic": t.topic,
                "status": "receiving" if raw is not None else "no_data",
                "data": raw,
            })
        return json.dumps({"motion_topics": results}, ensure_ascii=False, indent=2)

    @mcp.tool()
    async def motion_alerts(ctx: Context = None) -> str:
        """获取当前活跃运动告警。"""
        s = get_shared_state()
        warnings = s.event_log.get_active_warnings()
        motion_warnings = [
            w for w in warnings
            if "motion" in w.get("component_id", "").lower()
            or "gait" in w.get("component_id", "").lower()
        ]
        return json.dumps({
            "active_motion_alerts": len(motion_warnings),
            "alerts": motion_warnings,
        }, ensure_aware=False, indent=2)

    return mcp


def main():
    """独立运行单个 server（不通过 launcher）"""
    import asyncio, os
    from pathlib import Path
    init_kwargs = {
        "schema_path":  Path(os.getenv("MANASTONE_SCHEMA_PATH", "config/robot_schema.yaml")),
        "storage_dir":  Path(os.getenv("MANASTONE_STORAGE_DIR", "storage")),
        "robot_id":     os.getenv("MANASTONE_ROBOT_ID", "robot_01"),
        "mock_mode":    os.getenv("MANASTONE_MOCK_MODE", "false").lower() == "true",
    }
    mcp = create_server(**init_kwargs)
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = int(os.getenv("MANASTONE_PORT", "8080"))
    mcp.run(transport=os.getenv("MANASTONE_TRANSPORT", "sse"))
