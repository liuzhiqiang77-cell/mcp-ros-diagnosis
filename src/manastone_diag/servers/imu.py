"""
manastone-imu
IMU 姿态子系统 MCP Server

工具：
  posture_status  - 当前机体姿态（横滚、俯仰、偏航、角速度）
  posture_alerts  - 当前活跃姿态告警
  posture_history - 倾斜事件历史
  fall_risk       - 跌倒风险评估（综合多个指标）
"""
import json
import logging
import math
from contextlib import asynccontextmanager
from typing import AsyncIterator

from mcp.server.fastmcp import FastMCP, Context

from .base import AppState, init_shared_state, shutdown_shared_state, get_shared_state

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(server: FastMCP, **kwargs) -> AsyncIterator[AppState]:
    state = await init_shared_state(**kwargs)
    logger.info("manastone-imu ready")
    try:
        yield state
    finally:
        await shutdown_shared_state()


def create_server(**init_kwargs) -> FastMCP:
    from functools import partial
    mcp = FastMCP(
        "manastone-imu",
        lifespan=partial(_lifespan, **init_kwargs),
    )

    @mcp.tool()
    async def posture_status(ctx: Context = None) -> str:
        """
        获取当前机体姿态。
        包含横滚(roll)、俯仰(pitch)、偏航(yaw)角度（度），以及角速度。
        """
        s = get_shared_state()
        imu_topics = [
            t for t in s.schema.topics
            if t.message_protocol == "unitree_hg_imu"
        ]
        if not imu_topics:
            return json.dumps({"status": "no_imu_topic"}, ensure_ascii=False)

        raw = await s.dds_bridge.get_topic_data(imu_topics[0].topic)
        if raw is None:
            return json.dumps({"status": "no_data"}, ensure_ascii=False)

        imu = raw.get("imu_state", {})
        if not imu:
            return json.dumps({"status": "no_imu_state"}, ensure_ascii=False)

        rpy = imu.get("rpy", [0, 0, 0])
        gyro = imu.get("gyroscope", [0, 0, 0])
        accel = imu.get("accelerometer", [0, 0, 9.8])

        roll_deg  = math.degrees(rpy[0]) if len(rpy) > 0 else 0
        pitch_deg = math.degrees(rpy[1]) if len(rpy) > 1 else 0
        yaw_deg   = math.degrees(rpy[2]) if len(rpy) > 2 else 0

        def tilt_level(deg):
            deg = abs(deg)
            return "critical" if deg >= 30 else "warning" if deg >= 20 else "normal"

        return json.dumps({
            "roll_deg":    round(roll_deg, 2),
            "pitch_deg":   round(pitch_deg, 2),
            "yaw_deg":     round(yaw_deg, 2),
            "roll_level":  tilt_level(roll_deg),
            "pitch_level": tilt_level(pitch_deg),
            "gyro_x":      round(gyro[0], 4) if len(gyro) > 0 else None,
            "gyro_y":      round(gyro[1], 4) if len(gyro) > 1 else None,
            "gyro_z":      round(gyro[2], 4) if len(gyro) > 2 else None,
            "accel_z":     round(accel[2], 4) if len(accel) > 2 else None,
        }, ensure_ascii=False, indent=2)

    @mcp.tool()
    async def posture_alerts(ctx: Context = None) -> str:
        """获取当前活跃姿态告警。"""
        s = get_shared_state()
        warnings = s.event_log.get_active_warnings()
        imu_warnings = [
            w for w in warnings
            if w.get("component_id") == "imu_unit"
            or w.get("event_type", "").startswith("IMU_")
        ]
        return json.dumps({
            "active_posture_alerts": len(imu_warnings),
            "alerts": imu_warnings,
        }, ensure_ascii=False, indent=2)

    @mcp.tool()
    async def posture_history(limit: int = 20, ctx: Context = None) -> str:
        """获取姿态事件历史（倾斜超限记录）。"""
        s = get_shared_state()
        history = s.event_log.query_component_history("imu_unit", limit)
        return json.dumps({
            "component": "imu_unit",
            "event_count": len(history),
            "history": history,
        }, ensure_ascii=False, indent=2)

    @mcp.tool()
    async def fall_risk(ctx: Context = None) -> str:
        """
        综合评估当前跌倒风险。
        综合考虑倾斜角度、角速度、以及近期倾斜事件频率。
        """
        s = get_shared_state()
        imu_topics = [t for t in s.schema.topics if t.message_protocol == "unitree_hg_imu"]
        if not imu_topics:
            return json.dumps({"risk": "unknown", "reason": "无IMU数据"}, ensure_ascii=False)

        raw = await s.dds_bridge.get_topic_data(imu_topics[0].topic)
        if not raw:
            return json.dumps({"risk": "unknown", "reason": "无IMU数据"}, ensure_ascii=False)

        imu = raw.get("imu_state", {})
        rpy = imu.get("rpy", [0, 0, 0])
        gyro = imu.get("gyroscope", [0, 0, 0])

        roll  = abs(math.degrees(rpy[0])) if len(rpy) > 0 else 0
        pitch = abs(math.degrees(rpy[1])) if len(rpy) > 1 else 0
        max_tilt = max(roll, pitch)
        max_gyro = max(abs(g) for g in gyro) if gyro else 0

        # 近5分钟的倾斜事件数
        import time
        recent_events = s.event_log.query_recent(
            limit=100,
            since_ts=time.time() - 300,
            component_id="imu_unit",
        )
        recent_tilt_count = len(recent_events)

        # 风险评分
        risk_score = 0
        factors = []
        if max_tilt >= 30:
            risk_score += 40; factors.append(f"当前倾斜 {max_tilt:.1f}° ≥ 危险阈值30°")
        elif max_tilt >= 20:
            risk_score += 20; factors.append(f"当前倾斜 {max_tilt:.1f}° ≥ 警告阈值20°")
        if max_gyro >= 2.0:
            risk_score += 20; factors.append(f"角速度 {max_gyro:.2f} rad/s 较高")
        if recent_tilt_count >= 5:
            risk_score += 20; factors.append(f"5分钟内触发 {recent_tilt_count} 次倾斜告警")

        risk = "HIGH" if risk_score >= 40 else "MEDIUM" if risk_score >= 20 else "LOW"

        return json.dumps({
            "risk_level": risk,
            "risk_score": risk_score,
            "current_roll_deg": round(roll, 2),
            "current_pitch_deg": round(pitch, 2),
            "max_gyro_rad_s": round(max_gyro, 3),
            "recent_tilt_events_5min": recent_tilt_count,
            "contributing_factors": factors,
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
