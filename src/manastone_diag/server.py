"""
MCP Server 主入口
"""

import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from mcp.server.fastmcp import FastMCP, Context

from .config import get_config, Config
from .dds_bridge import DDSBridge
from .resources.joints import register_joints_resource

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@dataclass
class AppState:
    """应用生命周期状态，通过 ctx.request_context.lifespan_context 访问"""
    dds_bridge: DDSBridge
    config: Config


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppState]:
    """应用生命周期管理"""
    config = get_config()

    logger.info("🚀 Manastone Diagnostic 启动中...")
    logger.info(f"模式: {'模拟' if config.mock_mode else '真实DDS'}")

    # 初始化 DDS Bridge
    dds_bridge = DDSBridge()
    await dds_bridge.start()
    logger.info("✅ DDS Bridge 已启动")

    # 动态注册需要 dds_bridge 的资源
    register_joints_resource(server, dds_bridge)
    logger.info("✅ Joints Resource 已注册")

    try:
        yield AppState(dds_bridge=dds_bridge, config=config)
    finally:
        logger.info("🛑 正在关闭服务...")
        await dds_bridge.stop()
        logger.info("✅ DDS Bridge 已停止")


# 创建 MCP Server
mcp = FastMCP(
    "manastone-diagnostic",
    lifespan=app_lifespan,
    dependencies=["mcp", "gradio", "httpx", "pydantic", "pyyaml", "numpy"]
)


@mcp.tool()
async def diagnose(focus: str = "all", ctx: Context = None) -> str:
    """
    故障诊断工具 - 分析机器人健康状态
    
    Args:
        focus: 诊断焦点 ("all" | "joints" | "sensors" | "motion")
    
    Returns:
        JSON 格式的诊断报告
    """
    state: AppState = ctx.request_context.lifespan_context
    dds = state.dds_bridge

    # 获取关节状态
    joints = await dds.get_latest_joints()
    
    if not joints:
        return json.dumps({
            "status": "error",
            "message": "无法获取机器人数据"
        }, ensure_ascii=False)
    
    # 基础诊断逻辑
    report = {
        "timestamp": joints[0].timestamp if joints else None,
        "focus": focus,
        "summary": {},
        "anomalies": [],
        "recommendations": []
    }
    
    # 温度检查
    high_temp_joints = [j for j in joints if j.temperature > 50.0]
    critical_temp_joints = [j for j in joints if j.temperature > 65.0]
    
    if critical_temp_joints:
        report["summary"]["temperature"] = "critical"
        for j in critical_temp_joints:
            report["anomalies"].append({
                "type": "critical_temperature",
                "joint_id": j.joint_id,
                "value": j.temperature,
                "message": f"关节 {j.joint_id} 温度危险: {j.temperature:.1f}°C"
            })
    elif high_temp_joints:
        report["summary"]["temperature"] = "warning"
        for j in high_temp_joints:
            report["anomalies"].append({
                "type": "high_temperature",
                "joint_id": j.joint_id,
                "value": j.temperature,
                "message": f"关节 {j.joint_id} 温度偏高: {j.temperature:.1f}°C"
            })
    else:
        report["summary"]["temperature"] = "normal"
    
    # 生成建议
    if report["anomalies"]:
        report["recommendations"].append("检测到异常，建议检查相关关节")
        if any(a["type"] == "critical_temperature" for a in report["anomalies"]):
            report["recommendations"].append("温度危险！立即停止使用，等待冷却")
    else:
        report["recommendations"].append("当前状态正常")
    
    return json.dumps(report, ensure_ascii=False, indent=2)


@mcp.tool()
async def compare_joints(mode: str = "left_right", ctx: Context = None) -> str:
    """
    关节对比工具 - 对比左右对称关节
    
    Args:
        mode: 对比模式 ("left_right" | "history")
    
    Returns:
        JSON 格式的对比结果
    """
    state: AppState = ctx.request_context.lifespan_context
    dds = state.dds_bridge

    joints = await dds.get_latest_joints()
    if not joints:
        return json.dumps({"status": "error", "message": "无数据"}, ensure_ascii=False)
    
    joints_dict = {j.joint_id: j for j in joints}
    
    # 左右对称关节对
    pairs = [(0, 6), (1, 7), (2, 8), (3, 9), (4, 10), (5, 11),
             (14, 20), (15, 21), (16, 22), (17, 23), (18, 24), (19, 25)]
    
    results = []
    for left, right in pairs:
        if left in joints_dict and right in joints_dict:
            l, r = joints_dict[left], joints_dict[right]
            results.append({
                "joint_pair": f"{left}-{right}",
                "temp_diff": round(abs(l.temperature - r.temperature), 2),
                "torque_diff": round(abs(l.torque - r.torque), 2),
                "pos_diff": round(abs(l.position - r.position), 4)
            })
    
    return json.dumps({
        "status": "ok",
        "mode": mode,
        "comparisons": results
    }, ensure_ascii=False, indent=2)


@mcp.tool()
async def lookup_fault(fault_code: str, ctx: Context = None) -> str:
    """
    故障代码查询工具
    
    Args:
        fault_code: 故障代码 (如 "FK-001" 或 "Winding Overheated")
    
    Returns:
        故障详情
    """
    # TODO: 从 knowledge/fault_library.yaml 加载
    # 这里先返回模拟数据
    
    fault_db = {
        "FK-001": {
            "name": "关节编码器通信异常",
            "severity": "critical",
            "symptoms": ["关节位置反馈异常", "控制不稳定"],
            "causes": ["编码器线缆松动", "编码器损坏", "EMI干扰"],
            "repair_steps": [
                "1. 检查编码器线缆连接",
                "2. 重启关节驱动器",
                "3. 如仍异常，联系售后"
            ]
        },
        "FK-003": {
            "name": "关节过热保护",
            "severity": "warning", 
            "symptoms": ["电机温度超过阈值", "自动停机保护"],
            "causes": ["持续高负载", "散热不良", "环境温度过高"],
            "repair_steps": [
                "1. 等待 15-20 分钟自然冷却",
                "2. 检查通风口是否堵塞",
                "3. 降低任务负载或改善散热"
            ]
        }
    }
    
    # 模糊匹配
    for code, info in fault_db.items():
        if fault_code.upper() in code or fault_code.lower() in info["name"].lower():
            return json.dumps({
                "status": "found",
                "fault_code": code,
                **info
            }, ensure_ascii=False, indent=2)
    
    return json.dumps({
        "status": "not_found",
        "message": f"未找到故障代码: {fault_code}"
    }, ensure_ascii=False)


@mcp.resource("g1://system/health")
async def get_system_health() -> str:
    """获取系统整体健康状态"""
    return json.dumps({
        "status": "operational",
        "version": "0.1.0",
        "components": {
            "dds_bridge": "connected" if not get_config().mock_mode else "mock_mode",
            "cache": "active",
            "llm": "standby"
        }
    }, ensure_ascii=False, indent=2)



def main():
    """主入口"""
    config = get_config()

    logger.info(f"Starting Manastone Diagnostic Server...")
    logger.info(f"Transport: {config.server.transport}")
    logger.info(f"Address: {config.server.host}:{config.server.port}")

    # host/port 在 FastMCP 构造时设置；运行时只传 transport
    mcp.settings.host = config.server.host
    mcp.settings.port = config.server.port
    mcp.run(transport=config.server.transport)


if __name__ == "__main__":
    main()
