"""
ScenarioInterpreter — 自然语言 → MotionScenario

把研究员的自然语言描述翻译成结构化的 MotionScenario。

翻译策略（优先级从高到低）：
  1. 精确 ID 匹配：输入直接是 scenario_id（如 "stair_ascent"）
  2. LLM 翻译：调用配置的 LLMClient，理解语义后返回 JSON
  3. 关键词回退：无 LLM 时，用 ScenarioLibrary.keyword_match()
  4. 兜底构造：无任何匹配时，从描述中提取关节名/角度，构造临时场景

LLM 翻译的 Prompt 设计：
  - 系统提示提供所有场景 ID 和关键词
  - 要求 LLM 返回 JSON：{scenario_id, joint_override, setpoint_override, notes}
  - joint_override：如果用户指定了特定关节（如"右膝"），覆盖场景默认关节
  - setpoint_override：如果用户指定了具体角度，覆盖场景默认值
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from .scenario import ExperimentPhase, MotionScenario, ScenarioLibrary

if TYPE_CHECKING:
    from ..llm.client import LLMClient
    from ..schema.loader import RobotSchema

logger = logging.getLogger(__name__)

# 关节名称映射：中文 → 标准名（G1 命名）
_JOINT_NAME_MAP: Dict[str, str] = {
    # 腿部
    "左膝": "left_knee",      "右膝": "right_knee",
    "左髋": "left_hip_pitch", "右髋": "right_hip_pitch",
    "左踝": "left_ankle_pitch", "右踝": "right_ankle_pitch",
    "左髋滚": "left_hip_roll", "右髋滚": "right_hip_roll",
    # 手臂
    "左肘": "left_elbow",    "右肘": "right_elbow",
    "左肩": "left_shoulder_pitch", "右肩": "right_shoulder_pitch",
    "左腕": "left_wrist_pitch", "右腕": "right_wrist_pitch",
    # 腰部
    "腰": "waist_yaw", "腰部": "waist_pitch",
    # 四足（Go2/B1）
    "左前髋": "lf_hip",   "右前髋": "rf_hip",
    "左前腿": "lf_thigh", "右前腿": "rf_thigh",
    "左后腿": "lr_thigh", "右后腿": "rr_thigh",
    # xArm
    "第一轴": "joint1", "第二轴": "joint2", "第三轴": "joint3",
    "第四轴": "joint4", "第五轴": "joint5", "第六轴": "joint6",
    "第七轴": "joint7",
}

_SYSTEM_PROMPT_TEMPLATE = """你是一个机器人控制专家。你的任务是：将研究员的自然语言描述
翻译成结构化的运动场景配置，用于 PID 调参实验。

## 可用场景库
{scenario_list}

## 可用关节（当前机器人：{robot_type}）
{joint_list}

## 输出格式（严格 JSON）
{{
  "scenario_id": "最匹配的场景ID（必须是上面列表中的一个）",
  "joint_override": "如果用户指定了特定关节，填写标准关节名；否则 null",
  "setpoint_override_rad": 如果用户指定了具体角度（如30度），转换为弧度后填写；否则 null,
  "duration_override_s": 如果用户指定了时长，填写秒数；否则 null,
  "confidence": 0.0-1.0的匹配置信度,
  "reasoning": "选择该场景的理由（1-2句话）"
}}

## 规则
- 角度转换：1度 = 0.01745弧度
- 如果没有完全匹配的场景，选最接近的，confidence 填低一些
- 如果输入已经是 scenario_id（如 "stair_ascent"），直接返回该 ID，confidence=1.0
- joint_override 必须是可用关节列表中的标准名
"""


@dataclass
class InterpretResult:
    """解析结果"""
    scenario: MotionScenario
    confidence: float          # 0-1，LLM 或规则的匹配置信度
    method: str                # "exact_id" | "llm" | "keyword" | "fallback"
    reasoning: str             # 为什么选这个场景
    joint_override: Optional[str] = None


class ScenarioInterpreter:
    """
    自然语言 → MotionScenario 翻译器。

    依赖注入 LLMClient 和 RobotSchema（可选，用于验证关节名）。
    不可用时自动降级到关键词匹配。
    """

    def __init__(
        self,
        library: Optional[ScenarioLibrary] = None,
        llm_client: Optional["LLMClient"] = None,
        robot_schema: Optional["RobotSchema"] = None,
    ):
        self.library = library or ScenarioLibrary()
        self.llm = llm_client
        self.schema = robot_schema
        self._valid_joints: Optional[List[str]] = None

    def _get_valid_joints(self) -> List[str]:
        """从 schema 提取所有有效关节名"""
        if self._valid_joints is not None:
            return self._valid_joints
        if self.schema is None:
            return []
        joints = []
        for topic in self.schema.topics:
            for info in topic.motor_index_map.values():
                name = info.get("name")
                if name:
                    joints.append(name)
        self._valid_joints = joints
        return joints

    def _extract_joint_from_text(self, text: str) -> Optional[str]:
        """从文本中提取关节名（中文关键词 → 标准名）"""
        for cn, std in _JOINT_NAME_MAP.items():
            if cn in text:
                return std
        # 尝试直接匹配标准名
        valid = self._get_valid_joints()
        for joint in valid:
            if joint in text:
                return joint
        return None

    def _extract_angle_from_text(self, text: str) -> Optional[float]:
        """从文本中提取角度，支持度数和弧度两种格式"""
        # 匹配 "XX度" 或 "XX°"
        m = re.search(r"(\d+\.?\d*)\s*(?:度|°)", text)
        if m:
            deg = float(m.group(1))
            return round(deg * 0.017453, 3)
        # 匹配 "X.X rad" 或 "X.X弧度"
        m = re.search(r"(\d+\.?\d*)\s*(?:rad|弧度)", text)
        if m:
            return float(m.group(1))
        return None

    async def interpret(
        self,
        description: str,
        robot_type: Optional[str] = None,
    ) -> InterpretResult:
        """
        将自然语言描述翻译成 MotionScenario。

        Args:
            description: 自然语言描述，如 "模拟上楼梯时右膝关节响应"
            robot_type:  目标机器人类型（用于过滤场景）

        Returns:
            InterpretResult 包含场景、置信度、推理过程
        """
        # ── 策略 1：精确 ID 匹配 ──────────────────────────────
        exact = self.library.get(description.strip())
        if exact:
            return InterpretResult(
                scenario=exact,
                confidence=1.0,
                method="exact_id",
                reasoning=f"精确匹配场景 ID: {description}",
            )

        # ── 策略 2：LLM 翻译 ────────────────────────────────────
        if self.llm and self.llm.is_available():
            try:
                result = await self._llm_interpret(description, robot_type)
                if result and result.confidence >= 0.5:
                    return result
                logger.info("LLM 翻译置信度过低(%.2f)，尝试关键词回退", result.confidence if result else 0)
            except Exception as e:
                logger.warning("LLM 翻译失败：%s，降级到关键词匹配", e)

        # ── 策略 3：关键词回退 ──────────────────────────────────
        keyword_match = self.library.keyword_match(description)
        joint_override = self._extract_joint_from_text(description)
        angle_override = self._extract_angle_from_text(description)

        if keyword_match:
            scenario = keyword_match
            if joint_override:
                scenario = scenario.for_joint(joint_override)
            if angle_override:
                scenario = self._apply_setpoint_override(scenario, angle_override)
            return InterpretResult(
                scenario=scenario,
                confidence=0.6,
                method="keyword",
                reasoning=f"关键词匹配到场景 '{keyword_match.scenario_id}'",
                joint_override=joint_override,
            )

        # ── 策略 4：兜底构造 ────────────────────────────────────
        return self._fallback_construct(description, joint_override, angle_override)

    async def _llm_interpret(
        self,
        description: str,
        robot_type: Optional[str],
    ) -> Optional[InterpretResult]:
        """调用 LLM 解析场景"""
        rt = robot_type or (self.schema.robot_type if self.schema else "unknown")
        scenarios = self.library.for_robot(rt) if rt != "unknown" else self.library.all()

        scenario_list = "\n".join(
            f"- {s.scenario_id}: {s.name} | 关键词: {', '.join(s.keywords[:5])}"
            for s in scenarios
        )
        valid_joints = self._get_valid_joints()
        joint_list = ", ".join(valid_joints[:30]) if valid_joints else "（未知，请使用标准关节名）"

        system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
            scenario_list=scenario_list,
            robot_type=rt,
            joint_list=joint_list,
        )

        response = await self.llm.chat(
            user_message=f"将以下描述翻译为场景配置：\n{description}",
            system_prompt=system_prompt,
        )

        # 提取 JSON（LLM 可能包含多余文字）
        json_match = re.search(r"\{.*\}", response, re.DOTALL)
        if not json_match:
            logger.warning("LLM 返回无法解析的格式: %s", response[:200])
            return None

        parsed: Dict[str, Any] = json.loads(json_match.group())
        scenario_id = parsed.get("scenario_id", "")
        scenario = self.library.get(scenario_id)
        if not scenario:
            logger.warning("LLM 返回的 scenario_id '%s' 不存在", scenario_id)
            return None

        joint_override = parsed.get("joint_override")
        if joint_override:
            # 验证关节名有效性
            valid = self._get_valid_joints()
            if valid and joint_override not in valid:
                logger.warning("LLM 返回的关节名 '%s' 不在 schema 中", joint_override)
                joint_override = None
            else:
                scenario = scenario.for_joint(joint_override)

        setpoint = parsed.get("setpoint_override_rad")
        if setpoint is not None:
            scenario = self._apply_setpoint_override(scenario, float(setpoint))

        duration = parsed.get("duration_override_s")
        if duration is not None:
            scenario = self._apply_duration_override(scenario, float(duration))

        return InterpretResult(
            scenario=scenario,
            confidence=float(parsed.get("confidence", 0.7)),
            method="llm",
            reasoning=parsed.get("reasoning", "LLM 解析"),
            joint_override=joint_override,
        )

    def _fallback_construct(
        self,
        description: str,
        joint_override: Optional[str],
        angle_override: Optional[float],
    ) -> InterpretResult:
        """兜底：构造一个基础单阶段场景"""
        joint = joint_override or "left_knee"
        setpoint = angle_override or 0.5

        # 根据关节组选择合理的持续时长
        if any(arm in joint for arm in ["shoulder", "elbow", "wrist", "joint"]):
            duration = 1.5
            notes = "手臂关节，关注振荡和稳态误差"
        else:
            duration = 2.0
            notes = "腿部关节，平衡超调和响应速度"

        scenario = MotionScenario(
            scenario_id="custom",
            name=f"自定义场景: {description[:30]}",
            description=description,
            phases=[
                ExperimentPhase(
                    joint_name=joint,
                    setpoint_rad=setpoint,
                    duration_s=duration,
                    phase_label="custom_phase",
                    phase_notes=notes,
                )
            ],
            target_score_hint=80.0,
        )
        return InterpretResult(
            scenario=scenario,
            confidence=0.3,
            method="fallback",
            reasoning=f"未找到匹配场景，基于描述构造自定义实验（关节={joint}, 目标={setpoint:.2f}rad）",
            joint_override=joint_override,
        )

    @staticmethod
    def _apply_setpoint_override(scenario: MotionScenario, setpoint: float) -> MotionScenario:
        """用指定的目标角度覆盖场景所有阶段的 setpoint_rad"""
        new_phases = [
            ExperimentPhase(
                joint_name=p.joint_name,
                setpoint_rad=setpoint,
                duration_s=p.duration_s,
                initial_position_rad=p.initial_position_rad,
                phase_label=p.phase_label,
                phase_notes=p.phase_notes + f"（角度已覆盖为 {setpoint:.3f} rad）",
            )
            for p in scenario.phases
        ]
        return MotionScenario(
            scenario_id=scenario.scenario_id,
            name=scenario.name,
            description=scenario.description,
            phases=new_phases,
            robot_types=scenario.robot_types,
            joint_groups=scenario.joint_groups,
            target_score_hint=scenario.target_score_hint,
            keywords=scenario.keywords,
        )

    @staticmethod
    def _apply_duration_override(scenario: MotionScenario, duration: float) -> MotionScenario:
        """用指定时长覆盖场景所有阶段的 duration_s"""
        new_phases = [
            ExperimentPhase(
                joint_name=p.joint_name,
                setpoint_rad=p.setpoint_rad,
                duration_s=duration,
                initial_position_rad=p.initial_position_rad,
                phase_label=p.phase_label,
                phase_notes=p.phase_notes,
            )
            for p in scenario.phases
        ]
        return MotionScenario(
            scenario_id=scenario.scenario_id,
            name=scenario.name,
            description=scenario.description,
            phases=new_phases,
            robot_types=scenario.robot_types,
            joint_groups=scenario.joint_groups,
            target_score_hint=scenario.target_score_hint,
            keywords=scenario.keywords,
        )
