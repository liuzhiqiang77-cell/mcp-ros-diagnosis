"""
Schema Loader
解析 config/robot_schema.yaml，提供强类型的 schema 查询接口。

这是系统中唯一的"真相来源"——所有事件检测规则都从这里读取，
不允许在代码里硬编码阈值或组件名。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)


# ── 数据类定义 ────────────────────────────────────────────────────────────────

@dataclass
class Threshold:
    value: float
    direction: str  # above | below | not_equal

    def is_violated(self, current: float) -> bool:
        if self.direction == "above":
            return current > self.value
        if self.direction == "below":
            return current < self.value
        if self.direction == "not_equal":
            return current != self.value
        return False


@dataclass
class FieldRule:
    """一个字段的完整规则：从哪里读 → 属于哪个组件 → 什么情况产生什么事件"""
    path: str                         # 原始字段路径，如 "joints[*].temperature"
    component_template: str           # 组件ID模板，如 "leg_joint_{index}"
    component_id: Optional[str]       # 固定组件ID（非数组字段用）
    index_key: Optional[str]          # 数组索引字段名，如 "joint_id"
    unit: str
    semantic_type: str                # temperature | torque | voltage | ...
    description: str

    # 阈值 & 对应事件
    thresholds: Dict[str, Threshold]  # warning/critical/...
    events: Dict[str, str]            # warning/critical/recovery/change → event_type名

    # 内部用：上一次的状态，用于检测恢复
    _last_level: Dict[str, str] = field(default_factory=dict)

    def get_component_id(self, index: Any = None) -> str:
        """根据 index 解析出实际组件ID"""
        if self.component_id:
            return self.component_id
        if index is not None and "{index}" in self.component_template:
            return self.component_template.format(index=index)
        return self.component_template

    def evaluate(self, value: float, index: Any = None) -> Optional[str]:
        """
        对一个数值求值，返回应该产生的 event_type，或 None。
        内部维护上次状态，实现 recovery 检测。
        """
        key = str(index) if index is not None else "__single__"
        prev_level = self._last_level.get(key, "normal")

        # 优先检测最高级
        current_level = "normal"
        for level in ("critical", "warning"):
            t = self.thresholds.get(level)
            if t and t.is_violated(value):
                current_level = level
                break

        event_type = None

        if current_level != "normal" and current_level != prev_level:
            # 进入新的告警级别
            event_type = self.events.get(current_level)
        elif current_level == "normal" and prev_level != "normal":
            # 从告警恢复到正常
            event_type = self.events.get("recovery")

        self._last_level[key] = current_level
        return event_type


@dataclass
class TopicSchema:
    """一个 ROS2 话题的完整 schema"""
    topic: str
    description: str
    message_type: str
    message_protocol: str        # unitree_hg_lowstate | unitree_hg_bms | standard_joint_state | ...
    mock_scenario: str
    component_group: str
    poll_hz: float
    fields: List[FieldRule]
    motor_index_map: Dict[int, Dict]  # 仅 unitree_hg_lowstate 使用，来自SDK枚举


@dataclass
class ComponentInfo:
    """一个硬件组件实例"""
    component_id: str          # 如 "leg_joint_3"
    group: str                 # 如 "leg_joints"
    instance_key: str          # 如 "3"
    name: str                  # 如 "左膝关节"
    component_type: str        # servo_joint | power_management_unit | ...
    attributes: Dict[str, Any] # side, segment, dof 等


@dataclass
class EventTypeInfo:
    event_type: str
    severity: str
    description: str
    retention_days: int


@dataclass
class RobotSchema:
    """完整的机器人 schema"""
    robot_type: str
    schema_version: str
    topics: List[TopicSchema]
    components: Dict[str, ComponentInfo]   # component_id → ComponentInfo
    event_types: Dict[str, EventTypeInfo]  # event_type → EventTypeInfo
    pid_safety_bounds: Dict[str, Dict]     # joint_name → PID 安全边界配置（供 pid_tuner 使用）

    def get_topic(self, topic_name: str) -> Optional[TopicSchema]:
        return next((t for t in self.topics if t.topic == topic_name), None)

    def get_component(self, component_id: str) -> Optional[ComponentInfo]:
        return self.components.get(component_id)

    def get_event_type(self, event_type: str) -> Optional[EventTypeInfo]:
        return self.event_types.get(event_type)

    def all_topics(self) -> List[str]:
        return [t.topic for t in self.topics]

    def to_summary_dict(self) -> dict:
        """返回给 MCP tool 消费的 schema 摘要"""
        return {
            "robot_type": self.robot_type,
            "schema_version": self.schema_version,
            "topics": [
                {
                    "topic": t.topic,
                    "description": t.description,
                    "message_type": t.message_type,
                    "component_group": t.component_group,
                    "fields": [
                        {
                            "path": f.path,
                            "semantic_type": f.semantic_type,
                            "unit": f.unit,
                            "thresholds": {
                                k: {"value": v.value, "direction": v.direction}
                                for k, v in f.thresholds.items()
                            },
                            "events": f.events,
                        }
                        for f in t.fields
                    ],
                }
                for t in self.topics
            ],
            "component_count": len(self.components),
            "event_type_count": len(self.event_types),
        }


# ── Loader ────────────────────────────────────────────────────────────────────

class SchemaLoader:
    """
    从 config/robot_schema.yaml 加载 RobotSchema。

    也支持加载 discovery 生成的动态 schema（未知机器人自动发现后写入）。
    """

    def __init__(self, schema_path: str | Path):
        self.schema_path = Path(schema_path)

    def load(self) -> RobotSchema:
        if not self.schema_path.exists():
            raise FileNotFoundError(f"Schema 文件不存在: {self.schema_path}")

        with open(self.schema_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        topics = self._parse_topics(raw.get("topics", []))
        components = self._parse_components(raw.get("components", {}))
        # 从 motor_index_map 自动生成关节组件，合并进 components
        joint_components = self._generate_joint_components(raw.get("topics", []))
        components.update(joint_components)
        event_types = self._parse_event_types(raw.get("event_types", {}))
        pid_safety_bounds = raw.get("pid_safety_bounds", {})

        schema = RobotSchema(
            robot_type=raw.get("robot_type", "unknown"),
            schema_version=raw.get("schema_version", "0.0"),
            topics=topics,
            components=components,
            event_types=event_types,
            pid_safety_bounds=pid_safety_bounds,
        )
        logger.info(
            "Schema 加载完成: %s | %d 个话题 | %d 个组件 | %d 种事件类型",
            schema.robot_type, len(topics), len(components), len(event_types)
        )
        return schema

    # ── 内部解析方法 ────────────────────────────────────────────────────────

    def _parse_topics(self, raw_topics: list) -> List[TopicSchema]:
        result = []
        for rt in raw_topics:
            fields = self._parse_fields(rt.get("fields", []))
            # 解析 motor_index_map（key 转为 int）
            raw_map = rt.get("motor_index_map", {})
            motor_index_map = {int(k): v for k, v in raw_map.items()} if raw_map else {}
            result.append(TopicSchema(
                topic=rt["topic"],
                description=rt.get("description", ""),
                message_type=rt.get("message_type", "unknown"),
                message_protocol=rt.get("message_protocol", "unknown"),
                mock_scenario=rt.get("mock_scenario", ""),
                component_group=rt.get("component_group", ""),
                poll_hz=rt.get("poll_hz", 1.0),
                fields=fields,
                motor_index_map=motor_index_map,
            ))
        return result

    def _parse_fields(self, raw_fields: list) -> List[FieldRule]:
        result = []
        for rf in raw_fields:
            thresholds = {}
            for level, spec in (rf.get("thresholds") or {}).items():
                if isinstance(spec, dict) and "value" in spec:
                    thresholds[level] = Threshold(
                        value=float(spec["value"]),
                        direction=spec.get("direction", "above"),
                    )

            result.append(FieldRule(
                path=rf["path"],
                component_template=rf.get("component_template", ""),
                component_id=rf.get("component"),
                index_key=rf.get("index_key"),
                unit=rf.get("unit", ""),
                semantic_type=rf.get("semantic_type", ""),
                description=rf.get("description", ""),
                thresholds=thresholds,
                events=rf.get("events") or {},
            ))
        return result

    def _parse_components(self, raw_components: dict) -> Dict[str, ComponentInfo]:
        result = {}
        for group_name, group_def in raw_components.items():
            comp_type = group_def.get("type", "unknown")
            for key, attrs in group_def.get("instances", {}).items():
                component_id = f"{group_name}_{key}" if isinstance(key, int) else str(key)
                result[component_id] = ComponentInfo(
                    component_id=component_id,
                    group=group_name,
                    instance_key=str(key),
                    name=attrs.get("name", component_id),
                    component_type=comp_type,
                    attributes={k: v for k, v in attrs.items() if k != "name"},
                )
        return result

    def _generate_joint_components(self, raw_topics: list) -> Dict[str, ComponentInfo]:
        """
        从 motor_index_map 自动生成关节组件。
        这样就不需要在 components 节手写29个关节了。
        """
        result = {}
        for rt in raw_topics:
            index_map = rt.get("motor_index_map", {})
            if not index_map:
                continue
            for idx, info in index_map.items():
                name = info.get("name", f"joint_{idx}")
                component_id = f"joint_{name}"
                result[component_id] = ComponentInfo(
                    component_id=component_id,
                    group=info.get("group", "joints"),
                    instance_key=str(idx),
                    name=info.get("canonical", name),
                    component_type="servo_joint",
                    attributes={
                        "motor_index": idx,
                        "side": info.get("side", "center"),
                        "variant_29dof_only": info.get("variant_29dof_only", False),
                    },
                )
        return result

    def _parse_event_types(self, raw_events: dict) -> Dict[str, EventTypeInfo]:
        result = {}
        for event_type, spec in raw_events.items():
            result[event_type] = EventTypeInfo(
                event_type=event_type,
                severity=spec.get("severity", "INFO"),
                description=spec.get("description", ""),
                retention_days=spec.get("retention_days", 30),
            )
        return result
