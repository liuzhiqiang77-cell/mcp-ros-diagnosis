"""
Event Layer
- SemanticEvent: 语义事件数据类
- EventLog: 基于 SQLite 的 Append-Only 持久化存储
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── 事件数据类 ────────────────────────────────────────────────────────────────

@dataclass
class SemanticEvent:
    """
    一个语义事件。不可变，append-only。

    注意：这不是系统日志，是"有意义的状态变化"。
    触发条件由 robot_schema.yaml 定义，代码里不出现任何硬编码阈值。
    """
    event_type: str           # 对应 schema event_types 的 key
    robot_id: str
    component_id: str         # 产生事件的硬件组件，如 "leg_joint_3"
    component_name: str       # 人类可读名称，如 "左膝关节"
    severity: str             # INFO | WARNING | CRITICAL
    topic: str                # 来源话题
    field_path: str           # 来源字段路径
    semantic_type: str        # temperature | torque | ...
    value: float              # 触发时的数值
    unit: str
    threshold_value: Optional[float]  # 触发的阈值，None 表示状态变化类事件
    description: str

    # 自动填充
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ts: float = field(default_factory=time.time)
    prev_event_id: Optional[str] = None  # 因果链（同组件上一个事件）

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_log_line(self) -> str:
        """人类可读的单行摘要"""
        return (
            f"[{self.severity}] {self.event_type} | "
            f"{self.component_name} | "
            f"{self.semantic_type}={self.value}{self.unit}"
        )


# ── Append-Only SQLite Event Log ─────────────────────────────────────────────

class EventLog:
    """
    基于 SQLite 的 Append-Only 语义事件存储。

    与系统 log 的本质区别：
    - 强 schema（每条记录字段固定）
    - 不可修改（INSERT ONLY，没有 UPDATE/DELETE）
    - 因果链（prev_event_id 指向同组件上一条）
    - LLM 可直接查询（提供语义摘要接口）
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        # 内存中维护每个组件的最新 event_id，用于填充 prev_event_id
        self._last_event_id: Dict[str, str] = {}
        logger.info("EventLog 初始化: %s", self.db_path)

    def _init_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                event_id        TEXT PRIMARY KEY,
                event_type      TEXT NOT NULL,
                robot_id        TEXT NOT NULL,
                component_id    TEXT NOT NULL,
                component_name  TEXT NOT NULL,
                severity        TEXT NOT NULL,
                topic           TEXT NOT NULL,
                field_path      TEXT NOT NULL,
                semantic_type   TEXT NOT NULL,
                value           REAL NOT NULL,
                unit            TEXT NOT NULL,
                threshold_value REAL,
                description     TEXT NOT NULL,
                ts              REAL NOT NULL,
                prev_event_id   TEXT,
                hash            TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_events_component
                ON events(component_id, ts DESC);
            CREATE INDEX IF NOT EXISTS idx_events_severity
                ON events(severity, ts DESC);
            CREATE INDEX IF NOT EXISTS idx_events_type
                ON events(event_type, ts DESC);
            CREATE INDEX IF NOT EXISTS idx_events_ts
                ON events(ts DESC);
        """)
        self._conn.commit()

    def append(self, event: SemanticEvent) -> None:
        """追加一条事件，同时填充 prev_event_id 和 hash"""
        # 填充因果链
        prev_id = self._last_event_id.get(event.component_id)
        event.prev_event_id = prev_id

        # 计算 hash（防篡改，包含 prev_event_id）
        content = json.dumps({
            "event_type": event.event_type,
            "robot_id": event.robot_id,
            "component_id": event.component_id,
            "value": event.value,
            "ts": event.ts,
            "prev_event_id": event.prev_event_id,
        }, sort_keys=True)
        event_hash = hashlib.sha256(content.encode()).hexdigest()[:16]

        try:
            self._conn.execute("""
                INSERT INTO events (
                    event_id, event_type, robot_id, component_id, component_name,
                    severity, topic, field_path, semantic_type, value, unit,
                    threshold_value, description, ts, prev_event_id, hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                event.event_id, event.event_type, event.robot_id,
                event.component_id, event.component_name,
                event.severity, event.topic, event.field_path,
                event.semantic_type, event.value, event.unit,
                event.threshold_value, event.description,
                event.ts, event.prev_event_id, event_hash,
            ))
            self._conn.commit()
            self._last_event_id[event.component_id] = event.event_id
            logger.info("Event logged: %s", event.to_log_line())
        except Exception as e:
            logger.error("EventLog append failed: %s", e)

    def query_recent(
        self,
        limit: int = 50,
        severity: Optional[str] = None,
        component_id: Optional[str] = None,
        event_type: Optional[str] = None,
        since_ts: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """查询最近的事件，返回 dict 列表（供 MCP tool 直接序列化）"""
        conditions = []
        params: List[Any] = []

        if severity:
            conditions.append("severity = ?")
            params.append(severity)
        if component_id:
            conditions.append("component_id = ?")
            params.append(component_id)
        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)
        if since_ts:
            conditions.append("ts >= ?")
            params.append(since_ts)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(limit)

        cursor = self._conn.execute(
            f"SELECT * FROM events {where} ORDER BY ts DESC LIMIT ?",
            params,
        )
        return [dict(row) for row in cursor.fetchall()]

    def query_component_history(
        self, component_id: str, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """获取某组件的完整事件历史（按因果链顺序）"""
        cursor = self._conn.execute(
            "SELECT * FROM events WHERE component_id = ? ORDER BY ts ASC LIMIT ?",
            (component_id, limit),
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_active_warnings(self) -> List[Dict[str, Any]]:
        """
        获取当前活跃的告警（WARNING/CRITICAL）。
        活跃 = 有告警事件，但该组件还没有后续的 recovery 事件。
        """
        # 每个组件取最新一条，过滤掉已恢复的
        cursor = self._conn.execute("""
            SELECT e.*
            FROM events e
            INNER JOIN (
                SELECT component_id, MAX(ts) AS max_ts
                FROM events
                GROUP BY component_id
            ) latest ON e.component_id = latest.component_id
                     AND e.ts = latest.max_ts
            WHERE e.severity IN ('WARNING', 'CRITICAL')
            ORDER BY e.severity DESC, e.ts DESC
        """)
        return [dict(row) for row in cursor.fetchall()]

    def stats(self) -> Dict[str, Any]:
        """事件统计摘要"""
        total = self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        by_severity = dict(self._conn.execute(
            "SELECT severity, COUNT(*) FROM events GROUP BY severity"
        ).fetchall())
        active = len(self.get_active_warnings())
        return {
            "total_events": total,
            "by_severity": by_severity,
            "active_warnings": active,
        }

    def close(self) -> None:
        self._conn.close()
