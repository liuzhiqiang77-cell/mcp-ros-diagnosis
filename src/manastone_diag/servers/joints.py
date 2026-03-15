"""
manastone-joints
关节电机子系统 MCP Server

工具：
  joint_status       - 当前所有关节温度、力矩、速度、通信状态快照
  joint_alerts       - 当前活跃告警（仅关节类事件）
  joint_history      - 单个关节的完整事件历史（因果链）
  joint_compare      - 左右对称关节温度/力矩对比
  joint_schema       - 关节拓扑：motor_index_map + 阈值规则
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
    logger.info("manastone-joints ready")
    try:
        yield state
    finally:
        await shutdown_shared_state()


def create_server(**init_kwargs) -> FastMCP:
    from functools import partial
    mcp = FastMCP(
        "manastone-joints",
        lifespan=partial(_lifespan, **init_kwargs),
    )

    @mcp.tool()
    async def joint_status(
        group: str = "all",
        ctx: Context = None,
    ) -> str:
        """
        获取关节电机当前状态快照。

        Args:
            group: 关节组 "leg" | "waist" | "arm" | "head" | "all"
        """
        s = get_shared_state()
        schema = s.schema
        dds = s.dds_bridge

        joint_topics = [
            t for t in schema.topics
            if t.component_group == "body_joints"
        ]

        result = {"group": group, "joints": []}

        for topic_schema in joint_topics:
            raw = await dds.get_topic_data(topic_schema.topic)
            if raw is None:
                continue

            motor_state = raw.get("motor_state", [])
            index_map = topic_schema.motor_index_map

            for item in motor_state:
                idx = item.get("motor_index")
                info = index_map.get(int(idx)) if idx is not None else None
                if info is None:
                    continue
                if group != "all" and info.get("group") != group:
                    continue

                joint_entry = {
                    "motor_index": idx,
                    "joint_name": info["name"],
                    "canonical": info["canonical"],
                    "group": info["group"],
                    "side": info.get("side", "center"),
                    "temperature_c": item.get("temperature"),
                    "torque_nm": round(item.get("tau_est", 0), 2),
                    "velocity_rad_s": round(item.get("dq", 0), 3),
                    "position_rad": round(item.get("q", 0), 4),
                    "comm_lost": item.get("lost", 0),
                    "mode": item.get("mode", 0),
                }

                # 计算状态等级
                temp = item.get("temperature", 0)
                joint_entry["temp_level"] = (
                    "critical" if temp >= 70 else
                    "warning"  if temp >= 50 else
                    "normal"
                )
                joint_entry["comm_level"] = (
                    "critical" if item.get("lost", 0) > 0 else "normal"
                )

                result["joints"].append(joint_entry)

        result["total"] = len(result["joints"])
        result["anomalies"] = sum(
            1 for j in result["joints"]
            if j["temp_level"] != "normal" or j["comm_level"] != "normal"
        )
        return json.dumps(result, ensure_ascii=False, indent=2)

    @mcp.tool()
    async def joint_alerts(ctx: Context = None) -> str:
        """
        获取当前所有活跃关节告警（WARNING 或 CRITICAL 且未恢复）。
        这是关节诊断的首要入口。
        """
        s = get_shared_state()
        all_warnings = s.event_log.get_active_warnings()
        joint_warnings = [
            w for w in all_warnings
            if w.get("component_id", "").startswith("joint_")
        ]
        return json.dumps({
            "active_joint_alerts": len(joint_warnings),
            "alerts": joint_warnings,
        }, ensure_ascii=False, indent=2)

    @mcp.tool()
    async def joint_history(
        joint_name: str,
        limit: int = 20,
        ctx: Context = None,
    ) -> str:
        """
        查询某个关节的完整事件历史（按时间顺序，含因果链）。

        Args:
            joint_name: 关节名，如 "left_knee" 或 "right_hip_pitch"
            limit: 返回条数上限
        """
        s = get_shared_state()
        component_id = f"joint_{joint_name}"
        history = s.event_log.query_component_history(component_id, limit)
        comp = s.schema.get_component(component_id)
        return json.dumps({
            "component_id": component_id,
            "component_name": comp.name if comp else joint_name,
            "event_count": len(history),
            "history": history,
        }, ensure_ascii=False, indent=2)

    @mcp.tool()
    async def joint_compare(ctx: Context = None) -> str:
        """
        对比左右对称关节的温度和力矩差异。
        差异 > 5°C 或 > 5 Nm 会被标记为异常。
        """
        s = get_shared_state()
        schema = s.schema
        dds = s.dds_bridge

        joint_topics = [t for t in schema.topics if t.component_group == "body_joints"]
        joints_data = {}

        for topic_schema in joint_topics:
            raw = await dds.get_topic_data(topic_schema.topic)
            if not raw:
                continue
            for item in raw.get("motor_state", []):
                idx = item.get("motor_index")
                info = topic_schema.motor_index_map.get(int(idx)) if idx is not None else None
                if info:
                    joints_data[info["name"]] = item

        # 对称关节对
        pairs = [
            ("left_hip_pitch",    "right_hip_pitch"),
            ("left_hip_roll",     "right_hip_roll"),
            ("left_hip_yaw",      "right_hip_yaw"),
            ("left_knee",         "right_knee"),
            ("left_ankle_pitch",  "right_ankle_pitch"),
            ("left_ankle_roll",   "right_ankle_roll"),
            ("left_shoulder_pitch",  "right_shoulder_pitch"),
            ("left_shoulder_roll",   "right_shoulder_roll"),
            ("left_shoulder_yaw",    "right_shoulder_yaw"),
            ("left_elbow",           "right_elbow"),
            ("left_wrist_roll",      "right_wrist_roll"),
        ]

        comparisons = []
        for left_name, right_name in pairs:
            l = joints_data.get(left_name)
            r = joints_data.get(right_name)
            if l is None or r is None:
                continue
            temp_diff = abs(l.get("temperature", 0) - r.get("temperature", 0))
            torque_diff = abs(l.get("tau_est", 0) - r.get("tau_est", 0))
            entry = {
                "pair": f"{left_name} ↔ {right_name}",
                "temp_left_c": l.get("temperature"),
                "temp_right_c": r.get("temperature"),
                "temp_diff_c": round(temp_diff, 1),
                "torque_diff_nm": round(torque_diff, 2),
                "alert": None,
            }
            if temp_diff > 5.0:
                entry["alert"] = f"温度差异显著: {temp_diff:.1f}°C"
            elif torque_diff > 5.0:
                entry["alert"] = f"力矩差异显著: {torque_diff:.2f} Nm"
            comparisons.append(entry)

        flagged = [c for c in comparisons if c["alert"]]
        return json.dumps({
            "total_pairs": len(comparisons),
            "flagged_pairs": len(flagged),
            "comparisons": comparisons,
        }, ensure_ascii=False, indent=2)

    @mcp.tool()
    async def joint_schema(ctx: Context = None) -> str:
        """
        返回关节拓扑：motor_index_map（索引→关节名）、字段规则、阈值。
        用于 LLM 理解机器人关节结构。
        """
        s = get_shared_state()
        joint_topics = [
            t for t in s.schema.topics
            if t.component_group == "body_joints"
        ]
        result = []
        for t in joint_topics:
            result.append({
                "topic": t.topic,
                "message_protocol": t.message_protocol,
                "motor_index_map": {
                    str(k): v for k, v in t.motor_index_map.items()
                },
                "field_rules": [
                    {
                        "path": f.path,
                        "semantic_type": f.semantic_type,
                        "unit": f.unit,
                        "thresholds": {
                            level: {"value": th.value, "direction": th.direction}
                            for level, th in f.thresholds.items()
                        },
                        "events": f.events,
                    }
                    for f in t.fields
                ],
            })
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
