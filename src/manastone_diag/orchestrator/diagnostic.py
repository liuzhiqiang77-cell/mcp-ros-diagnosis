"""
诊断编排器 - 结合机器人状态 + 知识库 Skill + LLM，回答用户问题
"""

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

from ..llm import LLMClient
from ..semantic import describe_robot_state

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是 Manastone Diagnostic，Unitree G1 人形机器人的专属运维诊断助手。

你的工作方式：
1. 结合用户描述 + 当前机器人传感器状态，判断可能的故障
2. 从知识库中找到最相关的故障条目，给出有依据的诊断
3. 提供分步骤的处理建议（立即处理 / 短期 / 长期）

回答要求：
- 用中文回答，简洁直接
- 先给出判断（1-2句），再给操作建议
- 如果状态数据与用户描述一致，主动指出
- 不要编造数据，不确定时说明
"""

# 关键词 → skill文件ID 映射
_SKILL_KEYWORDS: dict[str, list[str]] = {
    "joint-overheat":      ["热", "温", "烫", "过热", "temperature", "散热", "发烫"],
    "gait-instability":    ["走", "步态", "偏", "不稳", "倒", "gait", "walk", "摔", "平衡"],
    "communication-fault": ["通信", "连接", "dds", "话题", "延迟", "心跳", "掉线", "断开"],
    "power-system":        ["电", "电池", "充电", "power", "电压", "断电", "欠压", "过压"],
    "sensor-calibration":  ["传感器", "imu", "摄像头", "相机", "激光", "标定", "漂移", "陀螺", "realsense"],
}


class DiagnosticOrchestrator:
    def __init__(self, llm: LLMClient, knowledge_dir: str,
                 skills_dir: str | None = None):
        self.llm = llm
        self.yaml_skills = self._load_yaml_skills(knowledge_dir)
        self.skill_files = self._load_skill_files(skills_dir)
        logger.info(
            f"已加载 {len(self.yaml_skills)} 条 YAML 故障知识, "
            f"{len(self.skill_files)} 份运维手册"
        )

    # ------------------------------------------------------------------
    # 数据加载
    # ------------------------------------------------------------------

    def _load_yaml_skills(self, knowledge_dir: str) -> list[dict]:
        path = Path(knowledge_dir) / "fault_library.yaml"
        if not path.exists():
            logger.warning(f"知识库文件不存在: {path}")
            return []
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data.get("faults", [])

    def _load_skill_files(self, skills_dir: str | None) -> list[dict]:
        """加载 ~/manastone/skills/ 下的 SKILL.md 文件"""
        if skills_dir is None:
            skills_dir = os.path.expanduser("~/manastone/skills")
        skills_path = Path(skills_dir)
        if not skills_path.exists():
            logger.warning(f"Skill 文档目录不存在: {skills_path}")
            return []

        result = []
        for skill_dir in sorted(skills_path.iterdir()):
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue

            content = skill_file.read_text(encoding="utf-8")

            # 解析元信息（前 25 行）
            meta = {"id": skill_dir.name, "name": skill_dir.name,
                    "category": "", "related_components": []}
            for line in content.splitlines()[:25]:
                if "**id**:" in line:
                    meta["id"] = line.split(":", 1)[1].strip()
                elif "**name**:" in line:
                    meta["name"] = line.split(":", 1)[1].strip()
                elif "**category**:" in line:
                    meta["category"] = line.split(":", 1)[1].strip()
                elif "**related_components**:" in line:
                    raw = line.split(":", 1)[1].strip()
                    try:
                        import json
                        meta["related_components"] = json.loads(raw)
                    except Exception:
                        pass

            # 生成摘要：去除 ASCII 框线图、代码块，保留文字段落
            excerpt = self._extract_text_excerpt(content, max_chars=2500)

            result.append({
                "id": meta["id"],
                "name": meta["name"],
                "category": meta["category"],
                "related_components": meta["related_components"],
                "content": content,    # 用于关键词匹配
                "excerpt": excerpt,    # 发给 LLM 的摘要
            })
            logger.info(f"已加载 Skill 文档: {meta['name']} ({skill_dir.name})")

        return result

    @staticmethod
    def _extract_text_excerpt(content: str, max_chars: int) -> str:
        """从 Markdown 中提取纯文字段落，去除 ASCII 框线和代码块"""
        lines = []
        in_code_block = False
        for line in content.splitlines():
            if line.startswith("```"):
                in_code_block = not in_code_block
                continue
            if in_code_block:
                continue
            # 去除 ASCII 框线字符
            if re.match(r'^[│┌└╔╗╚╝═─┬┴├┤┼╠╣╦╩╪╬▲▼\s]+$', line):
                continue
            lines.append(line)
        text = "\n".join(lines)
        # 折叠连续空行
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text[:max_chars]

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------

    def _find_relevant_yaml_skills(self, query: str, state_text: str) -> list[dict]:
        """基于关键词匹配找到最相关的 YAML 故障条目（最多 3 条）"""
        combined = (query + " " + state_text).lower()

        scored = []
        for skill in self.yaml_skills:
            score = 0
            for word in skill.get("name", "").split():
                if word in combined:
                    score += 3
            for symptom in skill.get("symptoms", []):
                for word in symptom.split():
                    if len(word) >= 2 and word in combined:
                        score += 2
            if skill.get("category", "") in combined:
                score += 2
            # 特定触发词
            if any(kw in combined for kw in ["温", "热", "烫", "temperature"]):
                if skill.get("id") in ("FK-003", "FK-002"):
                    score += 4
            if any(kw in combined for kw in ["编码", "通信", "encoder"]):
                if skill.get("id") == "FK-001":
                    score += 4
            if any(kw in combined for kw in ["位置", "跟踪", "偏", "漂"]):
                if skill.get("id") == "FK-007":
                    score += 4
            if any(kw in combined for kw in ["摄像", "相机", "深度", "realsense"]):
                if skill.get("id") == "FK-005":
                    score += 4
            if any(kw in combined for kw in ["激光", "点云", "lidar", "雷达"]):
                if skill.get("id") == "FK-004":
                    score += 4
            if any(kw in combined for kw in ["手", "灵巧", "手指"]):
                if skill.get("id") == "FK-008":
                    score += 4
            if any(kw in combined for kw in ["imu", "姿态", "漂移", "陀螺"]):
                if skill.get("id") == "FK-006":
                    score += 4
            if score > 0:
                scored.append((score, skill))

        scored.sort(key=lambda x: -x[0])
        return [s for _, s in scored[:3]]

    def _find_relevant_skill_files(self, query: str, state_text: str) -> list[dict]:
        """基于关键词匹配找到最相关的 SKILL.md 文档（最多 2 份）"""
        combined = (query + " " + state_text).lower()

        scored = []
        for skill in self.skill_files:
            score = 0
            skill_id = skill["id"]

            # 关键词映射
            for kw in _SKILL_KEYWORDS.get(skill_id, []):
                if kw in combined:
                    score += 3

            # 分类匹配
            if skill.get("category", "") in combined:
                score += 2

            # 名称匹配
            for word in skill.get("name", "").split():
                if len(word) >= 2 and word in combined:
                    score += 2

            # 相关组件匹配
            for comp in skill.get("related_components", []):
                if comp.lower() in combined:
                    score += 1

            # 全文关键词（低分值，避免噪音）
            for kw in _SKILL_KEYWORDS.get(skill_id, []):
                if kw in skill["content"].lower() and kw in combined:
                    score += 1

            if score > 0:
                scored.append((score, skill))

        scored.sort(key=lambda x: -x[0])
        return [s for _, s in scored[:2]]

    # ------------------------------------------------------------------
    # 格式化
    # ------------------------------------------------------------------

    def _format_yaml_skills(self, skills: list[dict]) -> str:
        if not skills:
            return "（未找到直接相关的故障知识条目）"

        lines = []
        for sk in skills:
            lines.append(f"## [{sk['id']}] {sk['name']} (严重度: {sk['severity']})")
            lines.append(f"根因说明: {sk.get('root_cause_explanation', '').strip()}")
            causes = sk.get("possible_causes", [])
            if causes:
                lines.append("可能原因: " + "；".join(causes))
            guide = sk.get("repair_guide", {})
            immediate = guide.get("immediate", [])
            if immediate:
                lines.append("立即处理: " + "；".join(immediate))
            short_term = guide.get("short_term", [])
            if short_term:
                lines.append("短期处理: " + "；".join(short_term))
            lines.append("")
        return "\n".join(lines)

    def _format_skill_files(self, skills: list[dict]) -> str:
        if not skills:
            return ""
        lines = []
        for sk in skills:
            lines.append(f"### 运维手册：{sk['name']}")
            lines.append(sk["excerpt"])
            lines.append("")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    async def handle_query(
        self, user_message: str, joints_status: dict[str, Any]
    ) -> str:
        """编排完整诊断流程：状态 → 语义化 → 检索知识 → LLM → 回复"""

        # 1. 语义化机器人状态
        state_text = describe_robot_state(joints_status)

        # 2. 检索相关 YAML 故障条目
        relevant_yaml = self._find_relevant_yaml_skills(user_message, state_text)
        yaml_text = self._format_yaml_skills(relevant_yaml)

        # 3. 检索相关运维手册
        relevant_files = self._find_relevant_skill_files(user_message, state_text)
        files_text = self._format_skill_files(relevant_files)

        # 4. 构建完整 prompt
        full_message = f"""用户问题：{user_message}

【当前机器人状态】
{state_text}

【结构化故障知识库】
{yaml_text}"""

        if files_text:
            full_message += f"\n\n【运维手册参考】\n{files_text}"

        # 5. 调用 LLM
        try:
            response = await self.llm.chat(full_message, system_prompt=SYSTEM_PROMPT)
            return response
        except Exception as e:
            logger.error(f"LLM 调用失败: {e}")
            return self._fallback_response(user_message, state_text, relevant_yaml)

    def _fallback_response(
        self, query: str, state_text: str, skills: list[dict]
    ) -> str:
        """LLM 不可用时的降级响应"""
        lines = ["**（LLM 不可用，基于规则响应）**", "", "**机器人当前状态：**", state_text]
        if skills:
            lines += ["", "**相关故障知识：**"]
            for sk in skills:
                lines.append(f"- [{sk['id']}] {sk['name']}: {', '.join(sk.get('possible_causes', []))}")
                guide = sk.get("repair_guide", {})
                for step in guide.get("immediate", []):
                    lines.append(f"  → {step}")
        return "\n".join(lines)
