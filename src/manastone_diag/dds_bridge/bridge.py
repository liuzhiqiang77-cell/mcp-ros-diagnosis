"""
DDS Bridge v2
基于 schema 驱动的多话题订阅器。

核心变化：
- 不再只订阅硬编码的几个话题，而是从 robot_schema.yaml 读取话题列表
- 每个话题独立缓存，数据格式为 Dict（通用，不依赖具体消息类型）
- mock 模式按话题生成对应的模拟数据
- 提供统一的 get_topic_data(topic) 接口给 EventDetector 调用
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from ..schema import RobotSchema

logger = logging.getLogger(__name__)


class TopicCache:
    """单个话题的滑动窗口缓存"""

    def __init__(self, window_seconds: int = 300, max_size: int = 600):
        self.window_seconds = window_seconds
        self._buffer: Deque[Dict[str, Any]] = deque(maxlen=max_size)

    def put(self, data: Dict[str, Any]) -> None:
        self._buffer.append({"ts": time.time(), "data": data})

    def latest(self) -> Optional[Dict[str, Any]]:
        if not self._buffer:
            return None
        return self._buffer[-1]["data"]

    def window(self, seconds: int) -> List[Dict[str, Any]]:
        cutoff = time.time() - seconds
        return [item["data"] for item in self._buffer if item["ts"] >= cutoff]


class DDSBridge:
    """
    Schema 驱动的 DDS 订阅桥接层。

    接口：
      await bridge.start()
      data = await bridge.get_topic_data(topic)   # 返回最新消息 dict
      await bridge.stop()
    """

    def __init__(self, schema: RobotSchema, mock_mode: bool = True):
        self.schema = schema
        self.mock_mode = mock_mode
        self._caches: Dict[str, TopicCache] = {
            t.topic: TopicCache() for t in schema.topics
        }
        self._running = False
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self._running = True
        if self.mock_mode:
            for topic_schema in self.schema.topics:
                task = asyncio.create_task(
                    self._mock_loop(topic_schema.topic, topic_schema.mock_scenario),
                    name=f"mock:{topic_schema.topic}",
                )
                self._tasks.append(task)
            logger.info("DDSBridge (mock) 已启动，模拟 %d 个话题", len(self._tasks))
        else:
            await self._start_real_dds()

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        logger.info("DDSBridge 已停止")

    async def get_topic_data(self, topic: str) -> Optional[Dict[str, Any]]:
        """获取某话题最新的消息（dict格式）"""
        cache = self._caches.get(topic)
        return cache.latest() if cache else None

    async def get_topic_window(self, topic: str, seconds: int = 60) -> List[Dict[str, Any]]:
        """获取某话题最近 N 秒的消息列表"""
        cache = self._caches.get(topic)
        return cache.window(seconds) if cache else []

    def get_all_latest(self) -> Dict[str, Optional[Dict[str, Any]]]:
        """获取所有话题的最新数据快照"""
        return {topic: cache.latest() for topic, cache in self._caches.items()}

    # ── Mock 数据生成 ────────────────────────────────────────────────────────

    async def _mock_loop(self, topic: str, scenario: str) -> None:
        generator = MOCK_GENERATORS.get(scenario, _mock_generic)
        state = {}  # 每个话题独立的生成器状态

        while self._running:
            try:
                data = generator(state)
                self._caches[topic].put(data)
            except Exception as e:
                logger.error("Mock generator error [%s]: %s", topic, e)
            await asyncio.sleep(0.5)  # 2Hz

    # ── 真实 DDS 订阅 ────────────────────────────────────────────────────────

    async def _start_real_dds(self) -> None:
        """
        真实 DDS 订阅入口。
        需要 CycloneDDS Python bindings + unitree_hg IDL。
        目前作为占位，实际订阅在此处实现。
        """
        logger.warning("真实 DDS 订阅暂未实现，回退到 mock 模式")
        self.mock_mode = True
        await self.start()


# ── Mock 数据生成器 ──────────────────────────────────────────────────────────
# 每个生成器接收可变 state dict，返回话题消息 dict
# state 用于维护跨次调用的连续状态（如温度缓慢上升）

def _make_joint_list(count: int, state: dict, temp_key: str = "temps") -> List[Dict]:
    if temp_key not in state:
        state[temp_key] = [35.0 + random.uniform(-3, 3) for _ in range(count)]
        state["overheat_joint"] = random.randint(0, count - 1)
        state["overheat_started"] = False

    temps = state[temp_key]
    overheat = state["overheat_joint"]

    # 模拟一个关节缓慢过热
    if not state["overheat_started"] and random.random() < 0.02:
        state["overheat_started"] = True
    if state["overheat_started"]:
        temps[overheat] = min(temps[overheat] + 0.3, 72.0)

    joints = []
    for i in range(count):
        joints.append({
            "joint_id": i,
            "position": random.uniform(-1.5, 1.5),
            "velocity": random.uniform(-2.0, 2.0),
            "torque": random.uniform(-15.0, 15.0),
            "temperature": round(temps[i] + random.uniform(-0.3, 0.3), 2),
            "error_code": 0,
        })
    return joints


def _mock_leg_joints(state: dict) -> Dict:
    return {"joints": _make_joint_list(12, state, "leg_temps")}

def _mock_waist_joints(state: dict) -> Dict:
    return {"joints": _make_joint_list(2, state, "waist_temps")}

def _mock_arm_joints(state: dict) -> Dict:
    return {"joints": _make_joint_list(14, state, "arm_temps")}

def _mock_head_joints(state: dict) -> Dict:
    return {"joints": _make_joint_list(3, state, "head_temps")}

def _mock_pmu(state: dict) -> Dict:
    if "soc" not in state:
        state["soc"] = 85.0
        state["voltage"] = 51.8
    # 模拟缓慢放电
    state["soc"] = max(state["soc"] - 0.01, 0.0)
    state["voltage"] = 42.0 + (state["soc"] / 100.0) * 12.0
    return {
        "battery_voltage": round(state["voltage"] + random.uniform(-0.1, 0.1), 2),
        "battery_current": round(8.0 + random.uniform(-1.0, 1.0), 2),
        "battery_soc": round(state["soc"], 1),
        "battery_temperature": round(32.0 + random.uniform(-1, 1), 1),
        "charge_state": 0,
    }

def _mock_generic(state: dict) -> Dict:
    return {"value": random.uniform(0, 100), "timestamp": time.time()}


def _mock_g1_lowstate(state: dict) -> Dict:
    """G1 unitree_hg LowState 格式 - motor_state[29] 位置性数组"""
    if "temps" not in state:
        state["temps"] = [35.0 + random.uniform(-3, 3) for _ in range(29)]
        state["overheat_idx"] = random.randint(0, 11)  # 随机腿部关节
        state["overheat_active"] = False
    temps = state["temps"]
    if not state["overheat_active"] and random.random() < 0.005:
        state["overheat_active"] = True
    if state["overheat_active"]:
        temps[state["overheat_idx"]] = min(temps[state["overheat_idx"]] + 0.4, 75.0)
    motor_state = []
    for i in range(29):
        motor_state.append({
            "motor_index": i,
            "mode": 1,
            "q": random.uniform(-1.5, 1.5),
            "dq": random.uniform(-2.0, 2.0),
            "ddq": random.uniform(-0.5, 0.5),
            "tau_est": random.uniform(-20.0, 20.0),
            "temperature": round(temps[i] + random.uniform(-0.3, 0.3), 1),
            "lost": 0,
        })
    return {
        "motor_state": motor_state,
        "power_v": round(50.0 + random.uniform(-1, 1), 2),
        "power_a": round(8.0 + random.uniform(-1, 1), 2),
        "bms_state": {"soc": 75.0, "temperature": [30.0, 31.0]},
        "imu_state": {
            "rpy": [random.uniform(-0.05, 0.05), random.uniform(-0.05, 0.05), 0.0],
            "gyroscope": [0.0, 0.0, 0.0],
            "accelerometer": [0.0, 0.0, 9.8],
        },
    }


MOCK_GENERATORS = {
    "g1_lowstate": _mock_g1_lowstate,
    "g1_bms":      _mock_g1_lowstate,   # 同一消息，不同协议解析器来切片
    "g1_imu":      _mock_g1_lowstate,
    # 保留旧的 Agibot X2 生成器供参考
    "leg_joints":   _mock_leg_joints,
    "waist_joints": _mock_waist_joints,
    "arm_joints":   _mock_arm_joints,
    "head_joints":  _mock_head_joints,
    "pmu":          _mock_pmu,
}
