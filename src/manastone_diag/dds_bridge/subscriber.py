"""
DDS Bridge - 订阅 G1 DDS 话题并缓存
"""

import json
import time
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List, Callable
from collections import deque
import threading
import logging

from ..config import get_config

logger = logging.getLogger(__name__)


@dataclass
class JointState:
    """关节状态"""
    joint_id: int
    position: float  # 弧度
    velocity: float  # 弧度/秒
    torque: float    # Nm
    temperature: float  # 摄氏度
    timestamp: float = field(default_factory=time.time)


@dataclass
class LowState:
    """G1 LowState 消息（简化版）"""
    timestamp: float
    joints: Dict[int, JointState] = field(default_factory=dict)
    battery_voltage: float = 0.0
    battery_current: float = 0.0
    error_code: int = 0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "joints": {k: {
                "position": v.position,
                "velocity": v.velocity,
                "torque": v.torque,
                "temperature": v.temperature,
            } for k, v in self.joints.items()},
            "battery_voltage": self.battery_voltage,
            "battery_current": self.battery_current,
            "error_code": self.error_code,
        }


class DataCache:
    """滑动窗口数据缓存"""
    
    def __init__(self, max_size: int = 1000, window_seconds: int = 300):
        self.max_size = max_size
        self.window_seconds = window_seconds
        self._cache: deque = deque(maxlen=max_size)
        self._lock = threading.Lock()
    
    def put(self, data: LowState) -> None:
        """放入新数据"""
        with self._lock:
            self._cache.append(data)
            # 清理过期数据
            cutoff_time = time.time() - self.window_seconds
            while self._cache and self._cache[0].timestamp < cutoff_time:
                self._cache.popleft()
    
    def get_latest(self) -> Optional[LowState]:
        """获取最新数据"""
        with self._lock:
            return self._cache[-1] if self._cache else None
    
    def get_window(self, seconds: int) -> List[LowState]:
        """获取最近 N 秒的数据"""
        with self._lock:
            cutoff_time = time.time() - seconds
            return [d for d in self._cache if d.timestamp >= cutoff_time]
    
    def get_trend(self, joint_id: int, seconds: int) -> Optional[Dict[str, float]]:
        """获取指定关节的趋势"""
        window = self.get_window(seconds)
        if not window:
            return None
        
        temps = [d.joints[joint_id].temperature for d in window if joint_id in d.joints]
        if not temps:
            return None
        
        return {
            "start": temps[0],
            "end": temps[-1],
            "min": min(temps),
            "max": max(temps),
            "avg": sum(temps) / len(temps),
        }


class DDSSubscriber:
    """DDS 订阅器"""
    
    def __init__(self):
        self.config = get_config()
        self.cache = DataCache(
            max_size=self.config.cache.max_size,
            window_seconds=self.config.cache.window_seconds
        )
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._callbacks: List[Callable[[LowState], None]] = []
        
        # 模拟数据模式
        if self.config.mock_mode:
            logger.info("运行在模拟数据模式")
            self._subscriber = None
        else:
            self._subscriber = None  # 实际 DDS 订阅器将在初始化时创建
    
    def register_callback(self, callback: Callable[[LowState], None]) -> None:
        """注册数据回调"""
        self._callbacks.append(callback)
    
    def start(self) -> None:
        """启动订阅"""
        if self._running:
            return
        
        self._running = True
        
        if self.config.mock_mode:
            self._thread = threading.Thread(target=self._mock_loop, daemon=True)
        else:
            self._thread = threading.Thread(target=self._dds_loop, daemon=True)
        
        self._thread.start()
        logger.info("DDS Bridge 已启动")
    
    def stop(self) -> None:
        """停止订阅"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        logger.info("DDS Bridge 已停止")
    
    def _dds_loop(self) -> None:
        """实际 DDS 订阅循环"""
        # TODO: 实现实际的 CycloneDDS 订阅
        # 需要等待 unitree_hg IDL 文件
        logger.warning("实际 DDS 订阅尚未实现，切换到模拟模式")
        self._mock_loop()
    
    def _mock_loop(self) -> None:
        """模拟数据循环（用于测试）"""
        import random
        
        joint_names = {
            0: "L_HIP_ROLL", 1: "L_HIP_YAW", 2: "L_HIP_PITCH",
            3: "L_KNEE", 4: "L_ANKLE_PITCH", 5: "L_ANKLE_ROLL",
            6: "R_HIP_ROLL", 7: "R_HIP_YAW", 8: "R_HIP_PITCH",
            9: "R_KNEE", 10: "R_ANKLE_PITCH", 11: "R_ANKLE_ROLL",
            12: "WAIST_YAW", 13: "L_SHOULDER_PITCH", 14: "L_SHOULDER_ROLL",
            15: "L_SHOULDER_YAW", 16: "L_ELBOW", 17: "L_WRIST",
            18: "R_SHOULDER_PITCH", 19: "R_SHOULDER_ROLL",
            20: "R_SHOULDER_YAW", 21: "R_ELBOW", 22: "R_WRIST",
        }
        
        # 模拟温度逐渐上升（模拟过热场景）
        base_temps = {i: 35.0 + random.uniform(-2, 2) for i in range(23)}
        
        while self._running:
            # 模拟左膝关节温度异常上升
            base_temps[3] += 0.5  # L_KNEE 温度持续上升
            
            joints = {}
            for joint_id in range(23):
                joints[joint_id] = JointState(
                    joint_id=joint_id,
                    position=random.uniform(-1.0, 1.0),
                    velocity=random.uniform(-0.5, 0.5),
                    torque=random.uniform(-10, 10),
                    temperature=base_temps[joint_id] + random.uniform(-0.5, 0.5),
                )
            
            state = LowState(
                timestamp=time.time(),
                joints=joints,
                battery_voltage=58.0 + random.uniform(-1, 1),
                battery_current=2.0 + random.uniform(-0.5, 0.5),
                error_code=0,
            )
            
            self.cache.put(state)
            
            # 触发回调
            for callback in self._callbacks:
                try:
                    callback(state)
                except Exception as e:
                    logger.error(f"回调执行失败: {e}")
            
            time.sleep(0.5)  # 2Hz
    
    def get_latest(self) -> Optional[LowState]:
        """获取最新状态"""
        return self.cache.get_latest()
    
    def get_joint_trend(self, joint_id: int, seconds: int = 600) -> Optional[Dict[str, float]]:
        """获取关节趋势"""
        return self.cache.get_trend(joint_id, seconds)


# 全局订阅器实例
_subscriber: Optional[DDSSubscriber] = None


def get_subscriber() -> DDSSubscriber:
    """获取全局订阅器"""
    global _subscriber
    if _subscriber is None:
        _subscriber = DDSSubscriber()
    return _subscriber
