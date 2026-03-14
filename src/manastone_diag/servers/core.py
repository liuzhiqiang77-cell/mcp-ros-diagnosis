"""
manastone-core
核心 Agent MCP Server

这是唯一包含 LLM 推理能力的 server。其他 server 只做数据读取和事件查询。

工具：
  system_status      - 全局健康总览（聚合所有子系统）
  active_warnings    - 当前所有活跃告警（跨所有组件）
  diagnose           - 自然语言故障诊断（LLM + 知识库 + 事件上下文）
  lookup_fault       - 故障代码查询
  schema_overview    - 机器人拓扑总览
  run_discovery      - 触发 ROS2 话题自动发现
  server_registry    - 列出当前启动的所有 MCP server
  recent_events      - 查询最近语义事件（支持过滤）
  event_stats        - EventLog 统计摘要
"""
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from mcp.server.fastmcp import FastMCP, Context

from .base import AppState, init_shared_state, shutdown_shared_state, get_shared_state

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(server: FastMCP, **kwargs) -> AsyncIterator[AppState]:
    state = await init_shared_state(**kwargs)
    logger.info("manastone-core ready")
    try:
        yield state
    finally:
        await shutdown_shared_state()


def create_server(**init_kwargs) -> FastMCP:
    from functools import partial
    mcp = FastMCP(
        "manastone-core",
        lifespan=partial(_lifespan, **init_kwargs),
    )

    @mcp.tool()
    async def system_status(ctx: Context = None) -> str:
        """
        全局系统健康总览。聚合所有子系统的当前状态，返回简洁摘要。
        这是诊断会话的推荐起点。
        """
        s = get_shared_state()
        all_warnings = s.event_log.get_active_warnings()
        stats = s.event_log.stats()

        # 按 severity 分组
        critical = [w for w in all_warnings if w.get("severity") == "CRITICAL"]
        warning  = [w for w in all_warnings if w.get("severity") == "WARNING"]

        # 按 component_group 分组
        groups: dict = {}
        for w in all_warnings:
            cid = w.get("component_id", "")
            comp = s.schema.get_component(cid)
            group = comp.group if comp else "unknown"
            if group not in groups:
                groups[group] = {"critical": 0, "warning": 0}
            if w.get("severity") == "CRITICAL":
                groups[group]["critical"] += 1
            else:
                groups[group]["warning"] += 1

        overall = (
            "CRITICAL" if critical else
            "WARNING"  if warning  else
            "NORMAL"
        )

        return json.dumps({
            "overall_health": overall,
            "robot_id": s.robot_id,
            "robot_type": s.schema.robot_type,
            "active_critical": len(critical),
            "active_warning": len(warning),
            "by_subsystem": groups,
            "event_stats": stats,
            "mock_mode": s.mock_mode,
        }, ensure_ascii=False, indent=2)

    @mcp.tool()
    async def active_warnings(
        severity: str = "",
        ctx: Context = None,
    ) -> str:
        """
        获取所有活跃告警（WARNING 或 CRITICAL 且未恢复）。

        Args:
            severity: 过滤 "WARNING" 或 "CRITICAL"，空则返回全部
        """
        s = get_shared_state()
        warnings = s.event_log.get_active_warnings()
        if severity:
            warnings = [w for w in warnings if w.get("severity") == severity.upper()]
        return json.dumps({
            "count": len(warnings),
            "warnings": warnings,
        }, ensure_ascii=False, indent=2)

    @mcp.tool()
    async def diagnose(
        query: str,
        ctx: Context = None,
    ) -> str:
        """
        自然语言故障诊断。结合当前活跃告警、组件状态快照和知识库，给出诊断和处理建议。

        Args:
            query: 问题描述，如 "左腿发烫" / "走路往右偏" / "电量掉得很快"
        """
        s = get_shared_state()

        # 收集上下文
        active_warnings = s.event_log.get_active_warnings()
        joint_raw = await s.dds_bridge.get_topic_data("/lf/lowstate")
        stats = s.event_log.stats()

        context = {
            "active_warnings": active_warnings[:15],
            "joint_snapshot": joint_raw,
            "event_stats": stats,
        }

        response = await s.orchestrator.handle_query(query, context)
        return response

    @mcp.tool()
    async def lookup_fault(
        fault_code: str,
        ctx: Context = None,
    ) -> str:
        """
        从故障知识库查询故障详情、可能原因和处理步骤。

        Args:
            fault_code: 故障代码或关键词，如 "FK-003" / "过热" / "通信丢失"
        """
        import yaml
        s = get_shared_state()
        from ..config import get_config
        config = get_config()
        yaml_path = Path(config.knowledge_dir) / "fault_library.yaml"

        if not yaml_path.exists():
            return json.dumps({"status": "error", "message": "故障库文件未找到"}, ensure_ascii=False)

        with open(yaml_path, encoding="utf-8") as f:
            faults = yaml.safe_load(f).get("faults", [])

        q = fault_code.lower()
        matched = [
            f for f in faults
            if q in f.get("id", "").lower()
            or q in f.get("name", "").lower()
            or any(q in sym.lower() for sym in f.get("symptoms", []))
        ]

        if not matched:
            return json.dumps({
                "status": "not_found",
                "query": fault_code,
                "available_ids": [f.get("id") for f in faults],
            }, ensure_ascii=False, indent=2)

        r = next((f for f in matched if q == f.get("id", "").lower()), matched[0])
        g = r.get("repair_guide", {})
        return json.dumps({
            "status": "found",
            "fault_code": r.get("id"),
            "name": r.get("name"),
            "severity": r.get("severity"),
            "symptoms": r.get("symptoms", []),
            "possible_causes": r.get("possible_causes", []),
            "immediate_actions": g.get("immediate", []),
            "short_term_actions": g.get("short_term", []),
            "long_term_actions": g.get("long_term", []),
            "root_cause_explanation": r.get("root_cause_explanation", "").strip(),
        }, ensure_ascii=False, indent=2)

    @mcp.tool()
    async def schema_overview(ctx: Context = None) -> str:
        """
        返回机器人 schema 总览：话题、组件、事件类型、字段规则摘要。
        用于 LLM 理解机器人结构。
        """
        s = get_shared_state()
        return json.dumps(s.schema.to_summary_dict(), ensure_ascii=False, indent=2)

    @mcp.tool()
    async def run_discovery(ctx: Context = None) -> str:
        """
        手动触发 ROS2 话题发现（真机模式）。
        生成 config/discovered_schema.yaml 草稿，供人工确认后用作 robot_schema.yaml。
        """
        s = get_shared_state()
        from ..discovery import ROS2Discovery
        disc = ROS2Discovery(mock_mode=s.mock_mode)
        out = s.schema_path.parent / "discovered_schema.yaml"
        try:
            await disc.generate_schema_yaml(out)
            return json.dumps({
                "status": "ok",
                "output_file": str(out),
                "message": "发现完成。请检查 discovered_schema.yaml 并补充阈值后用作 robot_schema.yaml。",
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)

    @mcp.tool()
    async def server_registry(ctx: Context = None) -> str:
        """
        列出当前所有已启动的 MCP Server 及其端口。
        用于 LLM 了解可用的诊断能力范围。
        """
        s = get_shared_state()
        return json.dumps({
            "active_servers": s.active_servers,
            "note": "每个 server 对应一个独立的硬件子系统",
        }, ensure_ascii=False, indent=2)

    @mcp.tool()
    async def recent_events(
        limit: int = 20,
        severity: str = "",
        component_id: str = "",
        event_type: str = "",
        ctx: Context = None,
    ) -> str:
        """
        查询最近的语义事件。

        Args:
            limit:        返回条数，默认20
            severity:     过滤 INFO / WARNING / CRITICAL
            component_id: 过滤特定组件，如 "joint_left_knee"
            event_type:   过滤特定事件类型，如 "JOINT_TEMP_CRITICAL"
        """
        s = get_shared_state()
        events = s.event_log.query_recent(
            limit=limit,
            severity=severity or None,
            component_id=component_id or None,
            event_type=event_type or None,
        )
        return json.dumps({"count": len(events), "events": events}, ensure_ascii=False, indent=2)

    @mcp.tool()
    async def event_stats(ctx: Context = None) -> str:
        """EventLog 统计摘要：总事件数、按严重度分布、当前活跃告警数。"""
        s = get_shared_state()
        return json.dumps(s.event_log.stats(), ensure_ascii=False, indent=2)

    return mcp


def main():
    """独立运行单个 server（不通过 launcher）"""
    import asyncio, os
    from pathlib import Path
    init_kwargs = {
        "schema_path":  Path(os.getenv("MANASTONE_SCHEMA_PATH", "config/robot_schema.yaml")),
        "storage_dir":  Path(os.getenv("MANASTONE_STORAGE_DIR", "storage")),
        "robot_id":     os.getenv("MANASTONE_ROBOT_ID", "robot_01"),
        "mock_mode":    os.getenv("MANASTONE_MOCK_MODE", "false").lower() == "true",
    }
    mcp = create_server(**init_kwargs)
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = int(os.getenv("MANASTONE_PORT", "8080"))
    mcp.run(transport=os.getenv("MANASTONE_TRANSPORT", "sse"))
