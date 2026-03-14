"""
manastone-hand
灵巧手子系统 MCP Server（可选，仅 G1 DEX3 等配置）

工具：
  hand_status     - 左右手当前状态（手指关节、抓力、通信）
  hand_alerts     - 当前活跃灵巧手告警
  hand_history    - 灵巧手事件历史
  grasp_test      - 抓握自检（检查各手指响应）
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
    logger.info("manastone-hand ready")
    try:
        yield state
    finally:
        await shutdown_shared_state()


def create_server(**init_kwargs) -> FastMCP:
    from functools import partial
    mcp = FastMCP(
        "manastone-hand",
        lifespan=partial(_lifespan, **init_kwargs),
    )

    @mcp.tool()
    async def hand_status(ctx: Context = None) -> str:
        """
        获取灵巧手当前状态（左右手关节、抓力传感器、通信健康）。
        如果机器人没有灵巧手，返回 not_available。
        """
        s = get_shared_state()
        hand_topics = [
            t for t in s.schema.topics
            if t.component_group == "dexterous_hand"
        ]
        if not hand_topics:
            return json.dumps({
                "status": "not_available",
                "message": "Schema 中未定义灵巧手话题。如果机器人配备了灵巧手，请在 robot_schema.yaml 中添加相应话题。",
            }, ensure_ascii=False, indent=2)

        result = {"hands": {}}
        for topic_schema in hand_topics:
            raw = await s.dds_bridge.get_topic_data(topic_schema.topic)
            if raw is None:
                result["hands"][topic_schema.component_group] = {"status": "no_data"}
                continue
            result["hands"][topic_schema.topic] = raw

        return json.dumps(result, ensure_ascii=False, indent=2)

    @mcp.tool()
    async def hand_alerts(ctx: Context = None) -> str:
        """获取灵巧手当前活跃告警。"""
        s = get_shared_state()
        warnings = s.event_log.get_active_warnings()
        hand_warnings = [
            w for w in warnings
            if "hand" in w.get("component_id", "").lower()
            or w.get("event_type", "").startswith("HAND_")
        ]
        return json.dumps({
            "active_hand_alerts": len(hand_warnings),
            "alerts": hand_warnings,
        }, ensure_ascii=False, indent=2)

    @mcp.tool()
    async def hand_history(
        side: str = "both",
        limit: int = 20,
        ctx: Context = None,
    ) -> str:
        """
        获取灵巧手事件历史。

        Args:
            side: "left" | "right" | "both"
            limit: 返回条数上限
        """
        s = get_shared_state()
        results = {}
        sides = ["left", "right"] if side == "both" else [side]
        for sd in sides:
            component_id = f"{sd}_hand"
            history = s.event_log.query_component_history(component_id, limit)
            results[sd] = {"event_count": len(history), "history": history}
        return json.dumps(results, ensure_ascii=False, indent=2)

    @mcp.tool()
    async def grasp_test(ctx: Context = None) -> str:
        """
        灵巧手自检：检查各手指关节通信和响应状态。
        注意：这是读取状态的自检，不会发送控制指令。
        """
        s = get_shared_state()
        hand_topics = [
            t for t in s.schema.topics
            if t.component_group == "dexterous_hand"
        ]
        if not hand_topics:
            return json.dumps({
                "status": "not_available",
                "message": "未配置灵巧手话题",
            }, ensure_ascii=False)

        # 读取最新数据，检查 lost 计数器
        issues = []
        for topic_schema in hand_topics:
            raw = await s.dds_bridge.get_topic_data(topic_schema.topic)
            if raw is None:
                issues.append(f"{topic_schema.topic}: 无数据")
                continue
            for item in raw.get("motor_state", []):
                if item.get("lost", 0) > 0:
                    issues.append(f"手指关节 {item.get('motor_index')} 通信丢失: lost={item['lost']}")

        return json.dumps({
            "test_result": "PASS" if not issues else "FAIL",
            "issues": issues,
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
