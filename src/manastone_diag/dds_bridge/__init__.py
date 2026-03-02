"""
DDS Bridge - CycloneDDS 订阅和缓存模块
处理与 G1 的 DDS 通信
"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Optional, Callable, Dict, List, Any
from collections import deque
import logging

from ..config import get_config

logger = logging.getLogger(__name__)


@dataclass
class JointState:
    """关节状态数据"""
    joint_id: int
    position: float  # 弧度
    velocity: float  # 弧度/秒
    torque: float    # Nm
    temperature: float  # °C
    timestamp: float = field(default_factory=time.time)


@dataclass
class LowState:
    """G1 LowState 消息结构"""
    # 根据 unitree_hg::msg::LowState 定义
    # 实际字段需要根据 IDL 文件调整
    level_flag: int = 0
    comm_version: int = 0
    robot_id: int = 0
    sn: List[int] = field(default_factory=lambda: [0]*2)
    bandwidth: int = 0
    motor_state: List[JointState] = field(default_factory=list)
    bms_state: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class DDSCache:
    """DDS 消息缓存 - 滑动窗口"""
    
    def __init__(self, max_size: int = 1000, window_seconds: int = 300):
        self.max_size = max_size
        self.window_seconds = window_seconds
        self._cache: deque = deque(maxlen=max_size)
        self._lock = asyncio.Lock()
    
    async def put(self, data: Any) -> None:
        """存入缓存"""
        async with self._lock:
            self._cache.append({
                "data": data,
                "timestamp": time.time()
            })
    
    async def get_recent(self, seconds: Optional[int] = None) -> List[Any]:
        """获取最近几秒的数据"""
        seconds = seconds or self.window_seconds
        cutoff = time.time() - seconds
        
        async with self._lock:
            return [
                item["data"] 
                for item in self._cache 
                if item["timestamp"] >= cutoff
            ]
    
    async def get_latest(self) -> Optional[Any]:
        """获取最新一条数据"""
        async with self._lock:
            if self._cache:
                return self._cache[-1]["data"]
            return None
    
    async def get_trend(self, field: str, seconds: int = 600) -> Dict[str, float]:
        """获取某字段的趋势数据"""
        recent = await self.get_recent(seconds)
        if not recent:
            return {"start": 0, "end": 0, "min": 0, "max": 0, "avg": 0}
        
        values = []
        for item in recent:
            if isinstance(item, dict) and field in item:
                values.append(item[field])
            elif hasattr(item, field):
                values.append(getattr(item, field))
        
        if not values:
            return {"start": 0, "end": 0, "min": 0, "max": 0, "avg": 0}
        
        return {
            "start": values[0],
            "end": values[-1],
            "min": min(values),
            "max": max(values),
            "avg": sum(values) / len(values)
        }


class MockDDSSubscriber:
    """模拟 DDS 订阅器 —— 基于 ScenarioEngine 的物理仿真"""

    def __init__(self):
        from .mock_scenarios import ScenarioEngine, ScenarioType
        self.callbacks: Dict[str, List[Callable]] = {}
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._engine = ScenarioEngine()
        self.ScenarioType = ScenarioType

    @property
    def scenario(self):
        return self._engine.scenario

    @scenario.setter
    def scenario(self, s):
        self._engine.scenario = s
        logger.info(f"场景切换 → {s}")

    def register_callback(self, topic: str, callback: Callable) -> None:
        if topic not in self.callbacks:
            self.callbacks[topic] = []
        self.callbacks[topic].append(callback)
        logger.info(f"已注册回调: {topic}")

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(f"模拟 DDS 订阅器已启动（场景: {self._engine.scenario}）")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("模拟 DDS 订阅器已停止")

    async def _loop(self) -> None:
        while self._running:
            try:
                joint_dicts = self._engine.step()

                motor_state = [
                    JointState(
                        joint_id=j["joint_id"],
                        position=j["position"],
                        velocity=j["velocity"],
                        torque=j["torque"],
                        temperature=j["temperature"],
                    )
                    for j in joint_dicts
                ]

                lowstate = LowState(
                    level_flag=1,
                    comm_version=1,
                    robot_id=1,
                    motor_state=motor_state,
                    timestamp=time.time(),
                )

                topic = "rt/lf/lowstate"
                if topic in self.callbacks:
                    for cb in self.callbacks[topic]:
                        try:
                            if asyncio.iscoroutinefunction(cb):
                                await cb(lowstate)
                            else:
                                cb(lowstate)
                        except Exception as e:
                            logger.error(f"回调执行错误: {e}")

                await asyncio.sleep(0.5)  # 2 Hz

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"生成模拟数据错误: {e}")
                await asyncio.sleep(1)


class DDSSubscriber:
    """真实 DDS 订阅器（需要 CycloneDDS）"""
    
    def __init__(self, domain_id: int = 0):
        self.domain_id = domain_id
        self.callbacks: Dict[str, List[Callable]] = {}
        self._running = False
        self._participant = None
    
    async def start(self) -> None:
        """启动 DDS 订阅"""
        try:
            from cyclonedds.domain import DomainParticipant
            from cyclonedds.topic import Topic
            from cyclonedds.sub import Subscriber, DataReader
            from cyclonedds.util import duration
            
            # TODO: 需要根据实际的 IDL 定义创建 Topic
            # 这里先占位，等拿到 unitree_hg IDL 后实现
            
            self._participant = DomainParticipant(self.domain_id)
            logger.info(f"DDS DomainParticipant 已创建 (domain_id={self.domain_id})")
            
            # TODO: 创建 Topic 和 DataReader
            
            self._running = True
            logger.info("DDS 订阅器已启动")
            
        except ImportError:
            logger.error("未安装 cyclonedds，请运行: pip install cyclonedds")
            raise
        except Exception as e:
            logger.error(f"DDS 启动错误: {e}")
            raise
    
    async def stop(self) -> None:
        """停止 DDS 订阅"""
        self._running = False
        if self._participant:
            # TODO: 清理资源
            pass
        logger.info("DDS 订阅器已停止")


class DDSBridge:
    """DDS Bridge - 统一接口"""
    
    def __init__(self):
        self.config = get_config()
        self.cache = DDSCache(
            max_size=self.config.cache.max_size,
            window_seconds=self.config.cache.window_seconds
        )
        self._subscriber: Optional[DDSSubscriber | MockDDSSubscriber] = None
    
    async def start(self) -> None:
        """启动 DDS Bridge"""
        if self.config.mock_mode:
            self._subscriber = MockDDSSubscriber()
        else:
            self._subscriber = DDSSubscriber(domain_id=self.config.dds.domain_id)
        
        # 注册缓存回调
        self._subscriber.register_callback(
            "rt/lf/lowstate", 
            self._on_lowstate
        )
        
        await self._subscriber.start()
    
    def set_scenario(self, scenario_name: str) -> bool:
        """切换 mock 场景（仅 mock 模式有效）"""
        if not isinstance(self._subscriber, MockDDSSubscriber):
            return False
        from .mock_scenarios import ScenarioType
        try:
            self._subscriber.scenario = ScenarioType(scenario_name)
            return True
        except ValueError:
            return False

    def get_scenario(self) -> str | None:
        if isinstance(self._subscriber, MockDDSSubscriber):
            return self._subscriber.scenario.value
        return None

    async def stop(self) -> None:
        """停止 DDS Bridge"""
        if self._subscriber:
            await self._subscriber.stop()
    
    async def _on_lowstate(self, data: LowState) -> None:
        """处理 LowState 消息"""
        await self.cache.put(data)
    
    async def get_latest_joints(self) -> Optional[List[JointState]]:
        """获取最新关节状态"""
        latest = await self.cache.get_latest()
        if latest and hasattr(latest, 'motor_state'):
            return latest.motor_state
        return None
    
    async def get_joint_trend(self, joint_id: int, field: str = "temperature", seconds: int = 600) -> Dict:
        """获取指定关节的趋势"""
        recent = await self.cache.get_recent(seconds)
        values = []
        
        for data in recent:
            if hasattr(data, 'motor_state'):
                for joint in data.motor_state:
                    if joint.joint_id == joint_id:
                        if hasattr(joint, field):
                            values.append(getattr(joint, field))
                        break
        
        if not values:
            return {"start": 0, "end": 0, "min": 0, "max": 0, "avg": 0, "count": 0}
        
        return {
            "start": values[0],
            "end": values[-1],
            "min": min(values),
            "max": max(values),
            "avg": sum(values) / len(values),
            "count": len(values)
        }
