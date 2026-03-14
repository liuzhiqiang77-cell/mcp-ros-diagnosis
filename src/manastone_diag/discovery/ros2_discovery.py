"""
ROS2 Topic Discovery
部署到机器人后，自动遍历所有可见的 ROS2 节点和话题，
生成一份初始 schema 草稿供人工确认或直接使用。

工作流程：
1. 调用 `ros2 topic list` 获取所有话题
2. 调用 `ros2 topic info <topic>` 获取消息类型
3. 调用 `ros2 topic echo --once <topic>` 采样一条消息，推断字段类型
4. 对照 component_hint_patterns 猜测硬件组件分类
5. 输出 discovered_schema.yaml（可直接用，也可作为 robot_schema.yaml 的基础）
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)


# ── 启发式规则：话题名 → 组件分类猜测 ────────────────────────────────────────

COMPONENT_HINT_PATTERNS = [
    (r"joint/leg",      "leg_joints",    "servo_joint"),
    (r"joint/waist",    "waist_joints",  "servo_joint"),
    (r"joint/arm",      "arm_joints",    "servo_joint"),
    (r"joint/hand",     "hand_joints",   "servo_joint"),
    (r"joint/head",     "head_joints",   "servo_joint"),
    (r"joint",          "joints",        "servo_joint"),
    (r"pmu|battery|power", "power_system", "power_management_unit"),
    (r"imu",            "imu",           "inertial_sensor"),
    (r"camera|image|depth|realsense", "vision", "camera"),
    (r"lidar|laser|scan", "lidar",       "lidar"),
    (r"selftest|diag",  "diagnostics",   "system"),
    (r"sport|motion|locomotion", "motion_controller", "controller"),
    (r"odom|estimator", "state_estimator", "estimator"),
]

# 字段名 → 语义类型猜测
FIELD_SEMANTIC_PATTERNS = [
    (r"temp",       "temperature", "celsius"),
    (r"torque|tau", "torque",      "Nm"),
    (r"current",    "current",     "A"),
    (r"voltage|vol","voltage",     "V"),
    (r"soc|charge_level", "state_of_charge", "percent"),
    (r"vel|velocity|speed", "velocity",  "rad/s"),
    (r"pos|position|angle", "position",  "rad"),
    (r"error|fault|status", "error_code","enum"),
    (r"force",      "force",       "N"),
]


@dataclass
class DiscoveredTopic:
    topic: str
    message_type: str
    component_group: str
    component_type: str
    sample: Optional[Dict[str, Any]]
    inferred_fields: List[Dict[str, Any]]


class ROS2Discovery:
    """
    ROS2 话题自动发现器。

    在真机模式下通过 subprocess 调用 ros2 CLI。
    在 mock 模式下返回预设的示例结果。
    """

    def __init__(self, mock_mode: bool = False, timeout: float = 5.0):
        self.mock_mode = mock_mode
        self.timeout = timeout

    async def discover_all(self) -> List[DiscoveredTopic]:
        """发现所有话题并推断 schema"""
        if self.mock_mode:
            return self._mock_discovery()

        topics = await self._list_topics()
        logger.info("ROS2 发现 %d 个话题，开始分析...", len(topics))

        results = []
        for topic in topics:
            try:
                msg_type = await self._get_topic_type(topic)
                sample = await self._sample_topic(topic)
                component_group, component_type = self._guess_component(topic)
                inferred = self._infer_fields(sample) if sample else []

                results.append(DiscoveredTopic(
                    topic=topic,
                    message_type=msg_type,
                    component_group=component_group,
                    component_type=component_type,
                    sample=sample,
                    inferred_fields=inferred,
                ))
                logger.debug("✓ %s → %s [%s]", topic, component_group, msg_type)
            except Exception as e:
                logger.warning("✗ %s: %s", topic, e)

        logger.info("Discovery 完成: %d 个话题已分析", len(results))
        return results

    async def generate_schema_yaml(self, output_path: str | Path) -> str:
        """
        运行 discovery 并生成 discovered_schema.yaml。
        返回生成的文件路径。
        """
        discovered = await self.discover_all()
        schema_dict = self._build_schema_dict(discovered)

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            yaml.dump(schema_dict, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

        logger.info("Schema 草稿已写入: %s", output_path)
        return str(output_path)

    # ── ROS2 CLI 调用 ────────────────────────────────────────────────────────

    async def _run(self, cmd: List[str]) -> str:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
            return stdout.decode("utf-8", errors="replace").strip()
        except asyncio.TimeoutError:
            proc.kill()
            return ""

    async def _list_topics(self) -> List[str]:
        out = await self._run(["ros2", "topic", "list"])
        return [t.strip() for t in out.splitlines() if t.strip()]

    async def _get_topic_type(self, topic: str) -> str:
        out = await self._run(["ros2", "topic", "info", topic])
        for line in out.splitlines():
            if "Type:" in line:
                return line.split(":", 1)[1].strip()
        return "unknown"

    async def _sample_topic(self, topic: str) -> Optional[Dict[str, Any]]:
        """采样一条消息，转成 dict"""
        out = await self._run(["ros2", "topic", "echo", "--once", "--csv", topic])
        if not out:
            return None
        # 尝试 JSON 解析（部分话题支持 --json）
        try:
            out_json = await self._run(["ros2", "topic", "echo", "--once", "--json", topic])
            return json.loads(out_json)
        except Exception:
            pass
        return {"_raw": out[:500]}

    # ── 推断逻辑 ────────────────────────────────────────────────────────────

    def _guess_component(self, topic: str) -> tuple[str, str]:
        lower = topic.lower()
        for pattern, group, comp_type in COMPONENT_HINT_PATTERNS:
            if re.search(pattern, lower):
                return group, comp_type
        return "unknown", "unknown"

    def _infer_fields(self, sample: Dict[str, Any], prefix: str = "") -> List[Dict[str, Any]]:
        """递归推断 sample 中的数值字段，生成 FieldRule 草稿"""
        results = []
        if not isinstance(sample, dict):
            return results

        for key, val in sample.items():
            path = f"{prefix}.{key}" if prefix else key

            if isinstance(val, (int, float)):
                semantic_type, unit = self._guess_semantic(key)
                results.append({
                    "path": path,
                    "unit": unit,
                    "semantic_type": semantic_type,
                    "description": f"Auto-discovered: {path}",
                    "thresholds": {},   # 留空，需人工填写
                    "events": {},
                    "_needs_review": True,  # 标记需要人工确认
                })
            elif isinstance(val, list) and val and isinstance(val[0], dict):
                # 数组展开
                array_path = f"{path}[*]"
                sub = self._infer_fields(val[0], prefix=array_path)
                results.extend(sub)
            elif isinstance(val, dict):
                results.extend(self._infer_fields(val, prefix=path))

        return results

    def _guess_semantic(self, field_name: str) -> tuple[str, str]:
        lower = field_name.lower()
        for pattern, semantic, unit in FIELD_SEMANTIC_PATTERNS:
            if re.search(pattern, lower):
                return semantic, unit
        return "unknown", ""

    def _build_schema_dict(self, discovered: List[DiscoveredTopic]) -> dict:
        """将 discovery 结果组装成 robot_schema.yaml 格式的 dict"""
        topics_list = []
        components_dict: Dict[str, Any] = {}

        for dt in discovered:
            topics_list.append({
                "topic": dt.topic,
                "description": f"Auto-discovered",
                "message_type": dt.message_type,
                "mock_scenario": dt.component_group,
                "component_group": dt.component_group,
                "poll_hz": 2,
                "fields": dt.inferred_fields,
                "_needs_review": True,
            })
            if dt.component_group not in components_dict:
                components_dict[dt.component_group] = {
                    "type": dt.component_type,
                    "description": f"Auto-discovered ({dt.component_type})",
                    "instances": {},
                }

        return {
            "robot_type": "auto_discovered",
            "schema_version": "0.0-draft",
            "_discovery_ts": time.time(),
            "_warning": "This schema was auto-generated. Review thresholds and events before production use.",
            "topics": topics_list,
            "components": components_dict,
            "event_types": {},
        }

    # ── Mock 模式 ────────────────────────────────────────────────────────────

    def _mock_discovery(self) -> List[DiscoveredTopic]:
        """Mock 模式：返回预设的 X2 话题列表"""
        mock_topics = [
            ("/aima/hal/joint/leg/state",   "aima_msgs/JointGroupState",   "leg_joints",    "servo_joint"),
            ("/aima/hal/joint/waist/state", "aima_msgs/JointGroupState",   "waist_joints",  "servo_joint"),
            ("/aima/hal/joint/arm/state",   "aima_msgs/JointGroupState",   "arm_joints",    "servo_joint"),
            ("/aima/hal/joint/head/state",  "aima_msgs/JointGroupState",   "head_joints",   "servo_joint"),
            ("/aima/hal/pmu/state",         "aima_msgs/PMUState",          "power_system",  "power_management_unit"),
            ("/aima/hal/imu/state",         "sensor_msgs/Imu",             "imu",           "inertial_sensor"),
            ("/camera/depth/image_raw",     "sensor_msgs/Image",           "vision",        "camera"),
        ]
        results = []
        for topic, msg_type, group, comp_type in mock_topics:
            results.append(DiscoveredTopic(
                topic=topic,
                message_type=msg_type,
                component_group=group,
                component_type=comp_type,
                sample=None,
                inferred_fields=[],
            ))
        return results
