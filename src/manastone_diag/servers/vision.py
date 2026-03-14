"""
manastone-vision
视觉子系统 MCP Server（M2 功能，当前为 stub）

工具：
  vision_status   - 相机和深度传感器健康状态
  vision_alerts   - 当前活跃视觉告警
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
    logger.info("manastone-vision ready (stub)")
    try:
        yield state
    finally:
        await shutdown_shared_state()


def create_server(**init_kwargs) -> FastMCP:
    from functools import partial
    mcp = FastMCP(
        "manastone-vision",
        lifespan=partial(_lifespan, **init_kwargs),
    )

    @mcp.tool()
    async def vision_status(ctx: Context = None) -> str:
        """
        获取视觉传感器健康状态（相机、深度传感器）。
        当前为 M2 stub，返回基础话题健康状态。
        """
        s = get_shared_state()
        vision_topics = [
            t for t in s.schema.topics
            if t.component_group == "vision"
        ]
        if not vision_topics:
            return json.dumps({
                "status": "not_configured",
                "message": "未在 robot_schema.yaml 中配置视觉话题",
                "m2_note": "完整视觉诊断功能将在 M2 实现",
            }, ensure_ascii=False, indent=2)

        results = []
        for t in vision_topics:
            raw = await s.dds_bridge.get_topic_data(t.topic)
            results.append({
                "topic": t.topic,
                "status": "receiving" if raw is not None else "no_data",
            })
        return json.dumps({"vision_topics": results}, ensure_ascii=False, indent=2)

    @mcp.tool()
    async def vision_alerts(ctx: Context = None) -> str:
        """获取当前活跃视觉告警。"""
        s = get_shared_state()
        warnings = s.event_log.get_active_warnings()
        vision_warnings = [
            w for w in warnings
            if "vision" in w.get("component_id", "").lower()
            or "camera" in w.get("component_id", "").lower()
        ]
        return json.dumps({
            "active_vision_alerts": len(vision_warnings),
            "alerts": vision_warnings,
        }, ensure_ascii=False, indent=2)

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
