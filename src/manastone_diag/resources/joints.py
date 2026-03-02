"""
Joints Resource - g1://joints/status
"""

from typing import Dict, List, Any, Optional
import logging

from mcp.server.fastmcp import FastMCP

from ..dds_bridge import DDSBridge, JointState
from ..config import get_config

logger = logging.getLogger(__name__)


class JointsResource:
    """关节状态资源"""
    
    # G1 关节名称映射（根据官方文档）
    JOINT_NAMES = {
        0: "左髋关节偏航",
        1: "左髋关节俯仰",
        2: "左髋关节滚转",
        3: "左膝关节",
        4: "左踝关节",
        5: "左踝关节滚转",
        6: "右髋关节偏航",
        7: "右髋关节俯仰",
        8: "右髋关节滚转",
        9: "右膝关节",
        10: "右踝关节",
        11: "右踝关节滚转",
        12: "腰部偏航",
        13: "腰部俯仰",
        14: "左肩关节偏航",
        15: "左肩关节俯仰",
        16: "左肩关节滚转",
        17: "左肘关节",
        18: "左腕关节",
        19: "左腕关节滚转",
        20: "右肩关节偏航",
        21: "右肩关节俯仰",
        22: "右肩关节滚转",
        23: "右肘关节",
        24: "右腕关节",
        25: "右腕关节滚转",
        26: "颈部偏航",
        27: "颈部俯仰",
        28: "颈部滚转",
    }
    
    # 异常阈值（根据手册和经验值）
    THRESHOLDS = {
        "temperature": {
            "warning": 50.0,   # °C - 黄色警告
            "critical": 65.0,  # °C - 红色危险
        },
        "torque": {
            "warning_percent": 70.0,  # 额定扭矩的百分比
            "critical_percent": 90.0,
        },
        "velocity": {
            "max": 10.0,  # rad/s
        }
    }
    
    def __init__(self, dds_bridge: DDSBridge):
        self.dds = dds_bridge
    
    async def get_status(self) -> Dict[str, Any]:
        """获取关节状态"""
        joints = await self.dds.get_latest_joints()
        
        if not joints:
            return {
                "status": "unavailable",
                "message": "暂无关节数据",
                "joints": []
            }
        
        # 转换并分析
        joint_data = []
        anomalies = []
        
        for joint in joints:
            name = self.JOINT_NAMES.get(joint.joint_id, f"Joint_{joint.joint_id}")
            
            # 检测异常
            temp_status = self._check_temperature(joint.temperature)
            
            joint_info = {
                "id": joint.joint_id,
                "name": name,
                "position_rad": round(joint.position, 4),
                "velocity_rad_s": round(joint.velocity, 4),
                "torque_nm": round(joint.torque, 2),
                "temperature_c": round(joint.temperature, 1),
                "status": temp_status["level"],
            }
            
            if temp_status["level"] != "normal":
                joint_info["alert"] = temp_status["message"]
                anomalies.append({
                    "joint_id": joint.joint_id,
                    "joint_name": name,
                    "type": "temperature",
                    "value": joint.temperature,
                    "threshold": temp_status["threshold"],
                    "level": temp_status["level"]
                })
            
            joint_data.append(joint_info)
        
        return {
            "status": "ok",
            "timestamp": joints[0].timestamp if joints else None,
            "joint_count": len(joint_data),
            "anomalies": anomalies,
            "anomaly_count": len(anomalies),
            "joints": joint_data
        }
    
    def _check_temperature(self, temp: float) -> Dict[str, Any]:
        """检查温度状态"""
        if temp >= self.THRESHOLDS["temperature"]["critical"]:
            return {
                "level": "critical",
                "threshold": self.THRESHOLDS["temperature"]["critical"],
                "message": f"温度 {temp:.1f}°C 超过危险阈值"
            }
        elif temp >= self.THRESHOLDS["temperature"]["warning"]:
            return {
                "level": "warning",
                "threshold": self.THRESHOLDS["temperature"]["warning"],
                "message": f"温度 {temp:.1f}°C 超过警告阈值"
            }
        else:
            return {
                "level": "normal",
                "threshold": self.THRESHOLDS["temperature"]["warning"],
                "message": "温度正常"
            }
    
    async def get_joint_detail(self, joint_id: int) -> Dict[str, Any]:
        """获取指定关节详情"""
        joints = await self.dds.get_latest_joints()
        
        if not joints:
            return {"status": "unavailable", "message": "暂无数据"}
        
        for joint in joints:
            if joint.joint_id == joint_id:
                name = self.JOINT_NAMES.get(joint_id, f"Joint_{joint_id}")
                
                # 获取趋势
                temp_trend = await self.dds.get_joint_trend(
                    joint_id, "temperature", seconds=600
                )
                torque_trend = await self.dds.get_joint_trend(
                    joint_id, "torque", seconds=600
                )
                
                return {
                    "status": "ok",
                    "joint_id": joint_id,
                    "name": name,
                    "current": {
                        "position_rad": round(joint.position, 4),
                        "velocity_rad_s": round(joint.velocity, 4),
                        "torque_nm": round(joint.torque, 2),
                        "temperature_c": round(joint.temperature, 1),
                    },
                    "trends": {
                        "temperature_10min": temp_trend,
                        "torque_10min": torque_trend,
                    }
                }
        
        return {"status": "error", "message": f"未找到关节 {joint_id}"}
    
    async def compare_symmetric(self) -> Dict[str, Any]:
        """对比左右对称关节"""
        joints = await self.dds.get_latest_joints()
        
        if not joints:
            return {"status": "unavailable", "message": "暂无数据"}
        
        # 左右对称关节对
        symmetric_pairs = [
            (0, 6),   # 左髋偏航 - 右髋偏航
            (1, 7),   # 左髋俯仰 - 右髋俯仰
            (2, 8),   # 左髋滚转 - 右髋滚转
            (3, 9),   # 左膝 - 右膝
            (4, 10),  # 左踝 - 右踝
            (5, 11),  # 左踝滚转 - 右踝滚转
            (14, 20), # 左肩偏航 - 右肩偏航
            (15, 21), # 左肩俯仰 - 右肩俯仰
            (16, 22), # 左肩滚转 - 右肩滚转
            (17, 23), # 左肘 - 右肘
            (18, 24), # 左腕 - 右腕
            (19, 25), # 左腕滚转 - 右腕滚转
        ]
        
        joints_dict = {j.joint_id: j for j in joints}
        comparisons = []
        
        for left_id, right_id in symmetric_pairs:
            if left_id in joints_dict and right_id in joints_dict:
                left = joints_dict[left_id]
                right = joints_dict[right_id]
                
                comparison = {
                    "joint_pair": f"{self.JOINT_NAMES.get(left_id)} - {self.JOINT_NAMES.get(right_id)}",
                    "temperature_diff": round(abs(left.temperature - right.temperature), 2),
                    "torque_diff": round(abs(left.torque - right.torque), 2),
                    "position_diff": round(abs(left.position - right.position), 4),
                }
                
                # 标记显著差异
                if comparison["temperature_diff"] > 5.0:
                    comparison["alert"] = "温度差异显著"
                elif comparison["torque_diff"] > 5.0:
                    comparison["alert"] = "扭矩差异显著"
                
                comparisons.append(comparison)
        
        return {
            "status": "ok",
            "comparison_count": len(comparisons),
            "comparisons": comparisons
        }


def register_joints_resource(mcp: FastMCP, dds_bridge: DDSBridge) -> None:
    """注册 joints resource 到 MCP Server"""
    resource = JointsResource(dds_bridge)
    
    @mcp.resource("g1://joints/status")
    async def get_joints_status() -> str:
        """获取所有关节状态"""
        data = await resource.get_status()
        import json
        return json.dumps(data, ensure_ascii=False, indent=2)
    
    @mcp.resource("g1://joints/detail/{joint_id}")
    async def get_joint_detail(joint_id: str) -> str:
        """获取指定关节详情"""
        data = await resource.get_joint_detail(int(joint_id))
        import json
        return json.dumps(data, ensure_ascii=False, indent=2)
    
    @mcp.resource("g1://joints/symmetric-comparison")
    async def get_symmetric_comparison() -> str:
        """获取左右对称关节对比"""
        data = await resource.compare_symmetric()
        import json
        return json.dumps(data, ensure_ascii=False, indent=2)
    
    logger.info("Joints Resource 已注册")
