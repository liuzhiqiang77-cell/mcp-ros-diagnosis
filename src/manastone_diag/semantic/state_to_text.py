"""
语义层 - 将原始关节数据转换成 LLM 可理解的自然语言描述
"""

from typing import Any


JOINT_NAMES = {
    0: "左髋偏航", 1: "左髋俯仰", 2: "左髋滚转",
    3: "左膝", 4: "左踝俯仰", 5: "左踝滚转",
    6: "右髋偏航", 7: "右髋俯仰", 8: "右髋滚转",
    9: "右膝", 10: "右踝俯仰", 11: "右踝滚转",
    12: "腰偏航", 13: "腰俯仰",
    14: "左肩偏航", 15: "左肩俯仰", 16: "左肩滚转",
    17: "左肘", 18: "左腕俯仰", 19: "左腕滚转",
    20: "右肩偏航", 21: "右肩俯仰", 22: "右肩滚转",
    23: "右肘", 24: "右腕俯仰", 25: "右腕滚转",
    26: "颈偏航", 27: "颈俯仰", 28: "颈滚转",
}


def describe_robot_state(status: dict[str, Any]) -> str:
    """
    将 JointsResource.get_status() 的输出转成简洁的中文描述。
    用于 LLM system prompt 的机器人状态部分。
    """
    if status.get("status") == "unavailable":
        return "当前无法获取机器人状态数据。"

    joint_count = status.get("joint_count", 0)
    anomalies = status.get("anomalies", [])
    joints = status.get("joints", [])

    lines = [f"当前共监测 {joint_count} 个关节。"]

    if not anomalies:
        # 汇总温度范围
        temps = [j["temperature_c"] for j in joints if "temperature_c" in j]
        if temps:
            lines.append(f"所有关节温度正常，范围 {min(temps):.1f}–{max(temps):.1f}°C。")
        lines.append("未检测到异常。")
        return "\n".join(lines)

    # 分级列出异常
    critical = [a for a in anomalies if a.get("level") == "critical"]
    warning = [a for a in anomalies if a.get("level") == "warning"]

    if critical:
        lines.append(f"⚠️ 危险异常 ({len(critical)} 个)：")
        for a in critical:
            lines.append(
                f"  - {a['joint_name']}（关节#{a['joint_id']}）: "
                f"温度 {a['value']:.1f}°C，超过危险阈值 {a['threshold']:.0f}°C"
            )
    if warning:
        lines.append(f"注意异常 ({len(warning)} 个)：")
        for a in warning:
            lines.append(
                f"  - {a['joint_name']}（关节#{a['joint_id']}）: "
                f"温度 {a['value']:.1f}°C，超过警告阈值 {a['threshold']:.0f}°C"
            )

    return "\n".join(lines)
