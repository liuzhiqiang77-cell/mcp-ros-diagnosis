"""
manastone-power
电源管理单元 MCP Server

工具：
  power_status    - 电池当前状态（电压、电流、SOC、温度）
  power_alerts    - 当前活跃电源告警
  power_history   - 电池事件历史
  charge_estimate - 基于当前放电率估算剩余工作时间
"""
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from mcp.server.fastmcp import FastMCP, Context

from .base import AppState, init_shared_state, shutdown_shared_state, get_shared_state

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(server: FastMCP, **kwargs) -> AsyncIterator[AppState]:
    state = await init_shared_state(**kwargs)
    logger.info("manastone-power ready")
    try:
        yield state
    finally:
        await shutdown_shared_state()


def create_server(**init_kwargs) -> FastMCP:
    from functools import partial
    mcp = FastMCP(
        "manastone-power",
        lifespan=partial(_lifespan, **init_kwargs),
    )

    @mcp.tool()
    async def power_status(ctx: Context = None) -> str:
        """
        获取电源管理单元当前状态。
        包含电池电压、放电电流、SOC、温度以及各项的健康等级。
        """
        s = get_shared_state()
        bms_topics = [
            t for t in s.schema.topics
            if t.message_protocol in ("unitree_hg_bms", "pmu")
        ]
        if not bms_topics:
            return json.dumps({"status": "no_power_topic", "message": "Schema 中未找到电源话题"}, ensure_ascii=False)

        raw = await s.dds_bridge.get_topic_data(bms_topics[0].topic)
        if raw is None:
            return json.dumps({"status": "no_data", "message": "暂无电源数据"}, ensure_ascii=False)

        voltage = raw.get("power_v", 0)
        current = raw.get("power_a", 0)
        bms = raw.get("bms_state", {})
        soc = bms.get("soc", 0) if isinstance(bms, dict) else 0
        temps = bms.get("temperature", []) if isinstance(bms, dict) else []
        bat_temp = temps[0] if temps else None

        def level(val, warn, crit, direction="above"):
            if direction == "above":
                return "critical" if val >= crit else "warning" if val >= warn else "normal"
            else:
                return "critical" if val <= crit else "warning" if val <= warn else "normal"

        return json.dumps({
            "voltage_v": round(voltage, 2),
            "voltage_level": level(voltage, 46.0, 43.0, "below"),
            "current_a": round(current, 2),
            "current_level": level(current, 20.0, 30.0, "above"),
            "soc_percent": round(soc, 1),
            "soc_level": level(soc, 20.0, 10.0, "below"),
            "battery_temp_c": round(bat_temp, 1) if bat_temp is not None else None,
            "temp_level": level(bat_temp, 45.0, 55.0) if bat_temp is not None else "unknown",
            "raw_bms": bms,
        }, ensure_ascii=False, indent=2)

    @mcp.tool()
    async def power_alerts(ctx: Context = None) -> str:
        """获取当前活跃电源告警（WARNING 或 CRITICAL 且未恢复）。"""
        s = get_shared_state()
        warnings = s.event_log.get_active_warnings()
        pmu_warnings = [
            w for w in warnings
            if w.get("component_id") == "battery_pack"
            or w.get("event_type", "").startswith("PMU_")
        ]
        return json.dumps({
            "active_power_alerts": len(pmu_warnings),
            "alerts": pmu_warnings,
        }, ensure_ascii=False, indent=2)

    @mcp.tool()
    async def power_history(limit: int = 20, ctx: Context = None) -> str:
        """
        获取电池事件历史。

        Args:
            limit: 返回条数上限
        """
        s = get_shared_state()
        history = s.event_log.query_component_history("battery_pack", limit)
        return json.dumps({
            "component": "battery_pack",
            "event_count": len(history),
            "history": history,
        }, ensure_ascii=False, indent=2)

    @mcp.tool()
    async def charge_estimate(ctx: Context = None) -> str:
        """
        基于当前 SOC 和放电电流估算剩余工作时间。
        注意：这是粗略估算，实际时间受工况影响较大。
        """
        s = get_shared_state()
        bms_topics = [
            t for t in s.schema.topics
            if t.message_protocol in ("unitree_hg_bms", "pmu")
        ]
        if not bms_topics:
            return json.dumps({"error": "无电源数据"}, ensure_ascii=False)

        raw = await s.dds_bridge.get_topic_data(bms_topics[0].topic)
        if raw is None:
            return json.dumps({"error": "暂无数据"}, ensure_ascii=False)

        voltage = raw.get("power_v", 48.0)
        current = raw.get("power_a", 0)
        bms = raw.get("bms_state", {})
        soc = bms.get("soc", 50) if isinstance(bms, dict) else 50

        # G1 电池容量约 288 Wh（官方数据）
        BATTERY_WH = 288.0
        remaining_wh = BATTERY_WH * (soc / 100.0)
        power_w = voltage * abs(current) if current > 0 else None

        result = {
            "soc_percent": round(soc, 1),
            "remaining_wh": round(remaining_wh, 1),
            "current_power_w": round(power_w, 1) if power_w else None,
        }

        if power_w and power_w > 0:
            hours = remaining_wh / power_w
            result["estimated_remaining_minutes"] = round(hours * 60, 0)
            result["confidence"] = "low"  # 实时工况波动较大
        else:
            result["estimated_remaining_minutes"] = None
            result["note"] = "当前放电电流为零或无效，无法估算"

        return json.dumps(result, ensure_ascii=False, indent=2)

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
