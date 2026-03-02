"""
测试 DDS Bridge 和 Joints Resource
"""

import sys
import time
import json
sys.path.insert(0, '../src')

from manastone_diag.config import set_config, Config
from manastone_diag.dds_bridge import get_subscriber
from manastone_diag.resources.joints import get_joints_resource


def test_dds_bridge():
    """测试 DDS Bridge"""
    print("🧪 测试 DDS Bridge...")
    
    # 配置模拟模式
    config = Config(mock_mode=True)
    set_config(config)
    
    # 启动订阅
    subscriber = get_subscriber()
    subscriber.start()
    
    print("   等待数据...")
    time.sleep(2)
    
    # 获取最新数据
    state = subscriber.get_latest()
    if state:
        print(f"   ✅ 获取到 {len(state.joints)} 个关节数据")
        print(f"   📊 左膝温度: {state.joints[3].temperature:.1f}°C")
    else:
        print("   ❌ 未获取到数据")
    
    # 停止
    subscriber.stop()
    print("   ✅ DDS Bridge 测试完成\n")


def test_joints_resource():
    """测试 Joints Resource"""
    print("🧪 测试 Joints Resource...")
    
    # 配置模拟模式
    config = Config(mock_mode=True)
    set_config(config)
    
    # 启动订阅
    subscriber = get_subscriber()
    subscriber.start()
    
    print("   等待数据...")
    time.sleep(2)
    
    # 读取资源
    resource = get_joints_resource()
    data = resource.read()
    
    print(f"   ✅ 状态: {data.get('status')}")
    print(f"   📊 关节数: {data.get('joint_count')}")
    print(f"   🌡️  平均温度: {data.get('summary', {}).get('avg_temperature', 0):.1f}°C")
    
    anomalies = data.get('anomalies', [])
    if anomalies:
        print(f"   ⚠️  异常数: {len(anomalies)}")
        for a in anomalies[:3]:
            print(f"      - {a.get('joint')}: {a.get('message')}")
    else:
        print("   ✅ 无异常")
    
    # 测试对比功能
    print("\n   测试左右对比...")
    comparison = resource.compare_sides()
    print(f"   📊 最大位置差异: {comparison.get('max_position_diff', 0):.4f}")
    print(f"   🌡️  最大温度差异: {comparison.get('max_temperature_diff', 0):.1f}°C")
    
    # 停止
    subscriber.stop()
    print("\n   ✅ Joints Resource 测试完成\n")


def main():
    """主测试"""
    print("=" * 50)
    print("🚀 Manastone Diagnostic 测试套件")
    print("=" * 50 + "\n")
    
    try:
        test_dds_bridge()
        test_joints_resource()
        
        print("=" * 50)
        print("✅ 所有测试通过！")
        print("=" * 50)
        
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    return 0


if __name__ == "__main__":
    exit(main())
