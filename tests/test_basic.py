"""
基础功能测试 - 对齐当前真实 API

运行方式:
    cd ~/manastone-diagnostic
    python tests/test_basic.py
"""

import asyncio
import json
import sys
import time

# 确保 src 在路径内（直接运行时需要）
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent / "src"))

from manastone_diag.config import set_config, Config
from manastone_diag.dds_bridge import DDSBridge
from manastone_diag.resources.joints import JointsResource


# ---------------------------------------------------------------------------
# 测试 1: DDS Bridge (Mock 模式)
# ---------------------------------------------------------------------------

async def test_dds_bridge():
    print("🧪 测试 DDS Bridge (Mock 模式)...")

    config = Config(mock_mode=True)
    set_config(config)

    bridge = DDSBridge()
    await bridge.start()

    print("   等待数据...")
    await asyncio.sleep(1.5)

    joints = await bridge.get_latest_joints()
    if joints:
        print(f"   ✅ 获取到 {len(joints)} 个关节数据")
        left_knee = next((j for j in joints if j.joint_id == 3), None)
        if left_knee:
            print(f"   🌡️  左膝 (ID=3) 温度: {left_knee.temperature:.1f}°C")
    else:
        print("   ❌ 未获取到数据")
        raise AssertionError("DDS Bridge 没有返回关节数据")

    await bridge.stop()
    print("   ✅ DDS Bridge 测试通过\n")


# ---------------------------------------------------------------------------
# 测试 2: JointsResource.get_status()
# ---------------------------------------------------------------------------

async def test_joints_resource_status():
    print("🧪 测试 JointsResource.get_status()...")

    config = Config(mock_mode=True)
    set_config(config)

    bridge = DDSBridge()
    await bridge.start()
    await asyncio.sleep(1.5)

    resource = JointsResource(bridge)
    data = await resource.get_status()

    print(f"   状态: {data.get('status')}")
    print(f"   关节总数: {data.get('joint_count')}")
    print(f"   异常数量: {data.get('anomaly_count', 0)}")

    anomalies = data.get("anomalies", [])
    if anomalies:
        print(f"   ⚠️  异常列表 (前3条):")
        for a in anomalies[:3]:
            print(f"      - {a.get('joint_name', a.get('joint_id'))}: "
                  f"{a.get('value', 0):.1f}°C [{a.get('level')}]")
    else:
        print("   ✅ 无异常")

    assert data.get("status") in ("ok", "warning", "critical"), \
        f"status 字段异常: {data.get('status')}"

    await bridge.stop()
    print("   ✅ get_status() 测试通过\n")


# ---------------------------------------------------------------------------
# 测试 3: JointsResource.compare_symmetric()
# ---------------------------------------------------------------------------

async def test_joints_resource_compare():
    print("🧪 测试 JointsResource.compare_symmetric()...")

    config = Config(mock_mode=True)
    set_config(config)

    bridge = DDSBridge()
    await bridge.start()
    await asyncio.sleep(1.5)

    resource = JointsResource(bridge)
    data = await resource.compare_symmetric()

    print(f"   状态: {data.get('status')}")
    comparisons = data.get("comparisons", [])
    print(f"   对比对数: {len(comparisons)}")

    if comparisons:
        # 找出温差最大的关节对
        worst = max(comparisons, key=lambda c: c.get("temperature_diff", 0))
        print(f"   最大温差: {worst.get('joint_pair')} → "
              f"{worst.get('temperature_diff', 0):.1f}°C")

    assert data.get("status") == "ok", f"compare_symmetric status 异常: {data.get('status')}"

    await bridge.stop()
    print("   ✅ compare_symmetric() 测试通过\n")


# ---------------------------------------------------------------------------
# 测试 4: lookup_fault 知识库加载
# ---------------------------------------------------------------------------

def test_fault_library():
    print("🧪 测试故障知识库加载...")

    import yaml
    from pathlib import Path
    from manastone_diag.config import get_config

    config = get_config()
    yaml_path = Path(config.knowledge_dir) / "fault_library.yaml"

    assert yaml_path.exists(), f"fault_library.yaml 不存在: {yaml_path}"

    with open(yaml_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    faults = data.get("faults", [])
    print(f"   故障条目数: {len(faults)}")
    for fault in faults:
        print(f"   [{fault['id']}] {fault['name']} ({fault['severity']})")

    assert len(faults) > 0, "故障库为空"
    print("   ✅ 故障知识库测试通过\n")


# ---------------------------------------------------------------------------
# 测试 5: Skills 目录加载
# ---------------------------------------------------------------------------

def test_skills_directory():
    print("🧪 测试 Skills 目录...")

    from pathlib import Path
    from manastone_diag.config import get_config

    config = get_config()
    skills_path = Path(config.knowledge_dir) / "skills"

    assert skills_path.exists(), f"skills 目录不存在: {skills_path}"

    skill_dirs = [d for d in skills_path.iterdir()
                  if d.is_dir() and (d / "SKILL.md").exists()]
    print(f"   发现 {len(skill_dirs)} 个 skill 文档:")
    for d in sorted(skill_dirs):
        print(f"   - {d.name}")

    assert len(skill_dirs) > 0, "skills 目录中没有 SKILL.md 文件"
    print("   ✅ Skills 目录测试通过\n")


# ---------------------------------------------------------------------------
# 测试 6: DiagnosticOrchestrator 初始化（不调用 LLM）
# ---------------------------------------------------------------------------

def test_orchestrator_init():
    print("🧪 测试 DiagnosticOrchestrator 初始化...")

    from unittest.mock import MagicMock
    from manastone_diag.config import get_config
    from manastone_diag.orchestrator import DiagnosticOrchestrator

    config = get_config()
    mock_llm = MagicMock()

    orchestrator = DiagnosticOrchestrator(mock_llm, config.knowledge_dir)

    print(f"   YAML 知识条目: {len(orchestrator.yaml_skills)}")
    print(f"   运维手册文档: {len(orchestrator.skill_files)}")

    assert len(orchestrator.yaml_skills) > 0, "YAML 知识库未加载"
    assert len(orchestrator.skill_files) > 0, "Skill 文档未加载"
    print("   ✅ DiagnosticOrchestrator 初始化测试通过\n")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

async def run_async_tests():
    await test_dds_bridge()
    await test_joints_resource_status()
    await test_joints_resource_compare()


def main():
    print("=" * 55)
    print("🚀 Manastone Diagnostic 测试套件")
    print("=" * 55 + "\n")

    # 设置 mock 模式
    set_config(Config(mock_mode=True))

    try:
        # 同步测试
        test_fault_library()
        test_skills_directory()
        test_orchestrator_init()

        # 异步测试
        asyncio.run(run_async_tests())

        print("=" * 55)
        print("✅ 全部测试通过！")
        print("=" * 55)
        return 0

    except AssertionError as e:
        print(f"\n❌ 断言失败: {e}")
        import traceback
        traceback.print_exc()
        return 1
    except Exception as e:
        print(f"\n❌ 测试异常: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
