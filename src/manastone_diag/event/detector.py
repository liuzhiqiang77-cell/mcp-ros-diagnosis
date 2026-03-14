"""
Event Detector
后台循环，持续对比缓存中的最新状态，
当字段值跨越 schema 定义的阈值时，产生 SemanticEvent 并写入 EventLog。

这一层完全由 robot_schema.yaml 驱动，代码里没有任何硬编码阈值。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

from ..schema import RobotSchema, FieldRule
from ..dds_bridge import DDSBridge
from .log import EventLog, SemanticEvent

logger = logging.getLogger(__name__)

# 话题数据超时判定（秒）
STALE_TIMEOUT_SECONDS = 10.0


class EventDetector:
    """
    基于 schema 的语义事件检测器。

    工作流程：
    1. 对每个 schema 中定义的话题，按 poll_hz 轮询 DDSBridge 缓存
    2. 解包消息中的字段值（支持数组展开）
    3. 调用 FieldRule.evaluate() 判断是否跨越阈值
    4. 产生 SemanticEvent 写入 EventLog
    """

    def __init__(
        self,
        schema: RobotSchema,
        dds_bridge: DDSBridge,
        event_log: EventLog,
        robot_id: str,
    ):
        self.schema = schema
        self.dds = dds_bridge
        self.event_log = event_log
        self.robot_id = robot_id
        self._running = False
        self._tasks: list[asyncio.Task] = []
        # 记录每个话题上次收到数据的时间，用于 STALE 检测
        self._last_data_ts: Dict[str, float] = {}
        self._stale_reported: Dict[str, bool] = {}

    async def start(self) -> None:
        self._running = True
        for topic_schema in self.schema.topics:
            interval = 1.0 / max(topic_schema.poll_hz, 0.1)
            task = asyncio.create_task(
                self._poll_loop(topic_schema.topic, interval),
                name=f"detector:{topic_schema.topic}",
            )
            self._tasks.append(task)
        logger.info("EventDetector 已启动，监控 %d 个话题", len(self._tasks))

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        logger.info("EventDetector 已停止")

    # ── 核心轮询循环 ────────────────────────────────────────────────────────

    async def _poll_loop(self, topic: str, interval: float) -> None:
        while self._running:
            try:
                await self._check_topic(topic)
            except Exception as e:
                logger.error("EventDetector error on %s: %s", topic, e)
            await asyncio.sleep(interval)

    async def _check_topic(self, topic: str) -> None:
        topic_schema = self.schema.get_topic(topic)
        if not topic_schema:
            return

        raw_data = await self.dds.get_topic_data(topic)

        # ── STALE 检测 ────────────────────────────────────────────────────
        now = time.time()
        if raw_data is None:
            last_ts = self._last_data_ts.get(topic, now)
            if (now - last_ts) > STALE_TIMEOUT_SECONDS and not self._stale_reported.get(topic):
                self._emit_system_event("TOPIC_DATA_STALE", topic, f"{topic} 超时未收到数据")
                self._stale_reported[topic] = True
            return
        else:
            if self._stale_reported.get(topic):
                self._emit_system_event("TOPIC_DATA_RECOVERED", topic, f"{topic} 数据恢复")
                self._stale_reported[topic] = False
            self._last_data_ts[topic] = now

        # ── 逐字段检测 ───────────────────────────────────────────────────
        for field_rule in topic_schema.fields:
            self._evaluate_field(topic, field_rule, raw_data)

    def _evaluate_field(
        self, topic: str, rule: "FieldRule", data: Dict[str, Any]
    ) -> None:
        """
        解析字段路径，展开数组，对每个值调用 rule.evaluate()。

        对于 unitree_hg_lowstate 协议，motor_state[] 数组里每个元素
        的 motor_index 字段就是 G1JointIndex 的值，通过 motor_index_map
        查到关节名，再生成 component_id，不需要任何硬编码。
        """
        topic_schema = self.schema.get_topic(topic)
        index_map = {}
        if topic_schema:
            index_map = {
                int(k): v
                for k, v in (getattr(topic_schema, 'motor_index_map', None) or {}).items()
            }

        path = rule.path
        if "[*]" in path:
            parts = path.split("[*].")
            array_key = parts[0]
            sub_key = parts[1] if len(parts) > 1 else None
            items = data.get(array_key, [])
            if not isinstance(items, list):
                return
            for item in items:
                if not isinstance(item, dict):
                    continue
                # motor_index 优先从元素里读，否则用 joint_id
                raw_index = item.get("motor_index") if "motor_index" in item else item.get(rule.index_key)
                value = item.get(sub_key) if sub_key else item
                if value is None or not isinstance(value, (int, float)):
                    continue
                # 解析 joint name
                joint_info = index_map.get(int(raw_index)) if raw_index is not None else None
                joint_name = joint_info["name"] if joint_info else str(raw_index)
                self._fire_if_needed(topic, rule, float(value), index=raw_index, joint_name=joint_name)
        else:
            value = data.get(path)
            if value is None or not isinstance(value, (int, float)):
                return
            self._fire_if_needed(topic, rule, float(value), index=None, joint_name=None)

    def _fire_if_needed(
        self,
        topic: str,
        rule: Any,
        value: float,
        index: Any,
        joint_name: Optional[str] = None,
    ) -> None:
        """调用 FieldRule.evaluate()，有返回值时写入 EventLog"""
        event_type = rule.evaluate(value, index)
        if not event_type:
            return

        # 优先用 joint_name 构造 component_id，否则 fallback 到 index
        if joint_name:
            component_id = f"joint_{joint_name}"
        else:
            component_id = rule.get_component_id(index)

        component = self.schema.get_component(component_id)
        component_name = component.name if component else (joint_name or component_id)

        event_type_info = self.schema.get_event_type(event_type)
        severity = event_type_info.severity if event_type_info else "INFO"
        description = event_type_info.description if event_type_info else event_type

        threshold_value: Optional[float] = None
        for level in ("critical", "warning"):
            t = rule.thresholds.get(level)
            if t and t.is_violated(value):
                threshold_value = t.value
                break

        event = SemanticEvent(
            event_type=event_type,
            robot_id=self.robot_id,
            component_id=component_id,
            component_name=component_name,
            severity=severity,
            topic=topic,
            field_path=rule.path,
            semantic_type=rule.semantic_type,
            value=value,
            unit=rule.unit,
            threshold_value=threshold_value,
            description=description,
        )
        self.event_log.append(event)

    def _emit_system_event(self, event_type: str, topic: str, description: str) -> None:
        event_type_info = self.schema.get_event_type(event_type)
        severity = event_type_info.severity if event_type_info else "WARNING"
        event = SemanticEvent(
            event_type=event_type,
            robot_id=self.robot_id,
            component_id=f"topic:{topic}",
            component_name=topic,
            severity=severity,
            topic=topic,
            field_path="__system__",
            semantic_type="system",
            value=0.0,
            unit="",
            threshold_value=None,
            description=description,
        )
        self.event_log.append(event)
