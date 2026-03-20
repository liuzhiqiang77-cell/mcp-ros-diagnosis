"""
PID 调参 Agent 循环 — AutoResearch 风格的"loop forever"架构

═══════════════════════════════════════════════════════════════
┌ 当前 pid_run_auto_tuning（Python 控制循环）：              ┐
│                                                            │
│   for i in range(max_iterations):                         │
│       params = llm.chat(history)   ← LLM 是子函数         │
│       result = run_experiment(params)                      │
│       if score >= target: break                            │
│                                                            │
└────────────────────────────────────────────────────────────┘

┌ PIDAgentLoop（LLM 控制循环）：                             ┐
│                                                            │
│   messages = [initial_task]                                │
│   while True:                          ← LLM 是主体        │
│       msg = llm.chat_with_tools(messages, tools)           │
│       if msg.tool_calls:                                   │
│           results = execute_tools(msg.tool_calls)          │
│           messages += [msg, results]                       │
│       else:                                                │
│           break  ← LLM 自己决定停止                        │
│                                                            │
└────────────────────────────────────────────────────────────┘

核心区别：控制权在 LLM 手里。
  - LLM 决定调用哪个工具（而不是 Python 强制每轮调一次 LLM）
  - LLM 决定何时停止（而不是 Python 检查 score >= target）
  - LLM 可以选择先 get_history、再 run_experiment、再多跑几次
  - LLM 的推理过程全部保留在 messages 里，每轮可以"看到"完整历史

这正是 AutoResearch 的 "propose → experiment → analyze → iterate" 闭环，
LLM 扮演的是"研究员"角色，而不是被调用的"计算器"。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .experiment import ExperimentConfig, ExperimentRunner
from .optimizer import TuningHistory
from .safety import SafetyGuard

logger = logging.getLogger(__name__)


# ── 暴露给 LLM 的工具定义（OpenAI function-calling 格式）─────
AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_experiment",
            "description": (
                "执行一次 PID 阶跃响应实验，返回完整的控制性能评分和诊断。"
                "每次调用都会自动记录到历史，供后续分析。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kp": {
                        "type": "number",
                        "description": "比例增益 Kp（必须在安全边界内）",
                    },
                    "ki": {
                        "type": "number",
                        "description": "积分增益 Ki（通常 0.0 ~ 5.0）",
                    },
                    "kd": {
                        "type": "number",
                        "description": "微分增益 Kd（通常 0.0 ~ 20.0）",
                    },
                    "reasoning": {
                        "type": "string",
                        "description": "你选择这组参数的工程依据（不超过80字）",
                    },
                },
                "required": ["kp", "ki", "kd", "reasoning"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_history",
            "description": "获取本次调参会话中所有历史实验记录，用于分析趋势。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": (
                "结束调参会话，提交最终推荐参数。"
                "当达到目标分数、或你认为已找到最优解时调用。"
                "调用后循环立即终止。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "final_kp": {"type": "number", "description": "最终推荐 Kp"},
                    "final_ki": {"type": "number", "description": "最终推荐 Ki"},
                    "final_kd": {"type": "number", "description": "最终推荐 Kd"},
                    "final_score": {"type": "number", "description": "该参数组合的得分"},
                    "conclusion": {
                        "type": "string",
                        "description": "调参结论：过程总结、参数含义、建议后续注意事项（100字以内）",
                    },
                },
                "required": ["final_kp", "final_ki", "final_kd", "final_score", "conclusion"],
            },
        },
    },
]

SYSTEM_PROMPT = """\
你是一位精通经典控制理论的 PID 调参研究员，正在用"实验→分析→假设→验证"的科研方法为机器人关节整定 PID 参数。

你有三个工具可以调用：
  run_experiment(kp, ki, kd, reasoning) — 运行一次阶跃响应实验，获取评分和诊断
  get_history()                          — 查看当前会话的全部历史实验
  finish(...)                            — 提交最终结论，结束调参

调参方法论（你必须遵循）：
1. 从保守参数出发，先整定 Kp 让系统响应，再加 Kd 抑制超调，最后用 Ki 消除稳态误差
2. 每次实验后仔细阅读诊断文字（diagnosis），它告诉你具体哪个维度有问题
3. 每次调整都要有明确的工程依据（填入 reasoning 字段），不能盲目猜测
4. 达到目标分数，或连续 3 次改善幅度 < 2 分时，调用 finish 结束

关键控制理论提示：
  超调 > 20%  → 减 Kp 或 增 Kd
  上升慢      → 增 Kp
  稳态误差大  → 增 Ki（Ki 每次增量不超过 0.1）
  振荡不停    → 大幅减 Kp，增 Kd，Ki 归零
"""


@dataclass
class AgentLoopResult:
    """Agent 循环的完整结果"""
    joint_name: str
    total_turns: int
    total_experiments: int
    elapsed_s: float
    best_score: float
    best_params: Dict[str, float]
    final_conclusion: str
    turn_log: List[Dict[str, Any]] = field(default_factory=list)
    aborted: bool = False
    abort_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "joint_name": self.joint_name,
            "total_turns": self.total_turns,
            "total_experiments": self.total_experiments,
            "elapsed_s": round(self.elapsed_s, 1),
            "best_score": round(self.best_score, 1),
            "best_params": self.best_params,
            "final_conclusion": self.final_conclusion,
            "aborted": self.aborted,
            "abort_reason": self.abort_reason,
            "turn_log": self.turn_log,
        }


class PIDAgentLoop:
    """
    AutoResearch 风格的 PID 调参 Agent 循环。

    LLM 是 while 循环的控制者：
      - 它决定每一步调用哪个工具
      - 它决定什么时候停（调用 finish）
      - 它的全部推理过程保留在 messages 历史中

    Python 只做两件事：
      - 执行 LLM 决定调用的工具
      - 在达到硬性限制（max_turns）时强制停止
    """

    def __init__(
        self,
        llm_client: Any,           # LLMClient
        runner: ExperimentRunner,
        history: TuningHistory,
        safety: SafetyGuard,
    ):
        self.llm = llm_client
        self.runner = runner
        self.history = history
        self.safety = safety

    async def run(
        self,
        joint_name: str,
        joint_group: str,
        target_score: float,
        max_turns: int,
        bounds: Any,               # PIDSafetyBounds
        setpoint_rad: float = 0.5,
        experiment_duration_s: float = 2.0,
        mock_mode: bool = True,
    ) -> AgentLoopResult:
        """
        启动 Agent 循环。

        Args:
            max_turns: 最大 LLM 对话轮数（注意：不是实验次数）
        """
        start_time = time.time()
        turn_log: List[Dict[str, Any]] = []
        best_score = 0.0
        best_params: Dict[str, float] = {}
        total_experiments = 0
        final_conclusion = ""

        # ── 初始任务消息 ──────────────────────────────────────
        initial_task = (
            f"关节：{joint_name}（组别：{joint_group}）\n"
            f"调参目标：找到综合评分 ≥ {target_score}/100 的 Kp/Ki/Kd 参数组合。\n"
            f"安全边界：Kp ∈ [{bounds.kp_min}, {bounds.kp_max}]  "
            f"Ki ∈ [{bounds.ki_min}, {bounds.ki_max}]  "
            f"Kd ∈ [{bounds.kd_min}, {bounds.kd_max}]\n"
            f"阶跃目标：{setpoint_rad} rad，实验时长：{experiment_duration_s}s\n\n"
            f"请开始调参。记住：先整定 Kp，再加 Kd，最后调 Ki。"
        )

        # messages 是整个会话的对话历史（LLM 每轮都能"看到"完整上下文）
        messages: List[Dict[str, Any]] = [
            {"role": "user", "content": initial_task}
        ]

        # ════════════════════════════════════════════════════
        # ← 这里是 AutoResearch 的 "loop forever" 核心
        #   LLM 驱动循环，Python 只是工具执行者
        # ════════════════════════════════════════════════════
        for turn in range(max_turns):
            turn_entry: Dict[str, Any] = {"turn": turn + 1, "actions": []}

            # ── 一次 LLM 调用（含完整历史上下文）────────────
            try:
                msg = await self.llm.chat_with_tools(
                    messages=messages,
                    tools=AGENT_TOOLS,
                    system_prompt=SYSTEM_PROMPT,
                )
            except Exception as e:
                logger.error("LLM 调用失败（turn=%d）: %s", turn + 1, e)
                return AgentLoopResult(
                    joint_name=joint_name,
                    total_turns=turn + 1,
                    total_experiments=total_experiments,
                    elapsed_s=time.time() - start_time,
                    best_score=best_score,
                    best_params=best_params,
                    final_conclusion=final_conclusion,
                    turn_log=turn_log,
                    aborted=True,
                    abort_reason=f"LLM 调用失败: {e}",
                )

            # 把 LLM 的回复追加到历史（无论是否有 tool_calls）
            assistant_msg = {k: v for k, v in msg.items() if not k.startswith("_")}
            messages.append(assistant_msg)

            finish_reason = msg.get("_finish_reason", "")
            tool_calls = msg.get("tool_calls") or []

            # LLM 选择直接回复（没有 tool_calls）→ 认为它结束了
            if not tool_calls:
                turn_entry["actions"].append({
                    "type": "text_response",
                    "content": msg.get("content", ""),
                })
                turn_log.append(turn_entry)
                logger.info("LLM 在 turn=%d 直接回复，结束循环", turn + 1)
                break

            # ── 依次执行 LLM 决定调用的每个工具 ───────────
            tool_results: List[Dict[str, Any]] = []
            should_finish = False

            for tc in tool_calls:
                tool_name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    args = {}

                action_entry: Dict[str, Any] = {
                    "type": "tool_call",
                    "tool": tool_name,
                    "args": args,
                }

                # ── 工具路由 ────────────────────────────────
                if tool_name == "run_experiment":
                    result_content, exp_score, exp_params = await self._execute_experiment(
                        joint_name=joint_name,
                        joint_group=joint_group,
                        args=args,
                        bounds=bounds,
                        setpoint_rad=setpoint_rad,
                        duration_s=experiment_duration_s,
                    )
                    total_experiments += 1
                    action_entry["score"] = exp_score
                    action_entry["params"] = exp_params

                    if exp_score > best_score:
                        best_score = exp_score
                        best_params = exp_params.copy()

                elif tool_name == "get_history":
                    records = self.history.recent(joint_name, 15)
                    result_content = json.dumps(records, ensure_ascii=False)
                    action_entry["records_returned"] = len(records)

                elif tool_name == "finish":
                    final_conclusion = args.get("conclusion", "")
                    # 使用 LLM 报告的最优参数（若得分更高则采用）
                    reported_score = args.get("final_score", 0.0)
                    if reported_score >= best_score:
                        best_score = reported_score
                        best_params = {
                            "kp": args.get("final_kp", 0),
                            "ki": args.get("final_ki", 0),
                            "kd": args.get("final_kd", 0),
                        }
                    result_content = "调参结束，感谢。"
                    action_entry["conclusion"] = final_conclusion
                    should_finish = True

                else:
                    result_content = f"未知工具: {tool_name}"

                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result_content,
                })
                turn_entry["actions"].append(action_entry)

            # 把工具结果追加到历史（LLM 下一轮可以看到）
            messages.extend(tool_results)
            turn_log.append(turn_entry)

            if should_finish:
                logger.info(
                    "LLM 调用 finish（turn=%d），最优得分=%.1f",
                    turn + 1, best_score,
                )
                break

        # ── 超出 max_turns 强制停止 ───────────────────────
        elapsed = time.time() - start_time
        total_turns = len(turn_log)
        aborted = total_turns >= max_turns and not should_finish  # type: ignore[possibly-undefined]

        if not final_conclusion:
            best = self.history.best(joint_name)
            if best:
                final_conclusion = (
                    f"达到最大轮数限制（{max_turns}轮），"
                    f"历史最优：Kp={best.get('kp', '?')} Ki={best.get('ki', '?')} "
                    f"Kd={best.get('kd', '?')}，得分={best.get('score', '?')}"
                )

        return AgentLoopResult(
            joint_name=joint_name,
            total_turns=total_turns,
            total_experiments=total_experiments,
            elapsed_s=elapsed,
            best_score=best_score,
            best_params=best_params,
            final_conclusion=final_conclusion,
            turn_log=turn_log,
            aborted=aborted,
            abort_reason="达到最大轮数" if aborted else "",
        )

    async def _execute_experiment(
        self,
        joint_name: str,
        joint_group: str,
        args: Dict[str, Any],
        bounds: Any,
        setpoint_rad: float,
        duration_s: float,
    ) -> Tuple[str, float, Dict[str, float]]:
        """
        执行 LLM 请求的实验，返回 (tool_result_str, score, params)。
        包含安全校验——LLM 可能会无视边界，这里兜底拒绝。
        """
        kp = float(args.get("kp", 1.0))
        ki = float(args.get("ki", 0.0))
        kd = float(args.get("kd", 0.0))

        # 安全校验：LLM 有时忽略边界约束，这里强制钳制而非拒绝
        kp_clamped = max(bounds.kp_min, min(bounds.kp_max, kp))
        ki_clamped = max(bounds.ki_min, min(bounds.ki_max, ki))
        kd_clamped = max(bounds.kd_min, min(bounds.kd_max, kd))
        was_clamped = (kp != kp_clamped or ki != ki_clamped or kd != kd_clamped)

        config = ExperimentConfig(
            joint_name=joint_name,
            joint_group=joint_group,
            kp=kp_clamped,
            ki=ki_clamped,
            kd=kd_clamped,
            setpoint_rad=setpoint_rad,
            duration_s=duration_s,
        )
        result = await self.runner.run(config)

        # 存历史（供 get_history 工具和下一轮 LLM 参考）
        self.history.save(joint_name, {
            "experiment_id": result.experiment_id,
            "timestamp": result.timestamp,
            "kp": kp_clamped, "ki": ki_clamped, "kd": kd_clamped,
            "score": result.metrics.score,
            "grade": result.metrics.grade,
            "overshoot_pct": result.metrics.overshoot_pct,
            "rise_time_s": result.metrics.rise_time_s,
            "settling_time_s": result.metrics.settling_time_s,
            "sse_pct": result.metrics.sse_pct,
            "oscillation_count": result.metrics.oscillation_count,
            "diagnosis": result.metrics.diagnosis,
            "reasoning": args.get("reasoning", ""),
        })

        # 构造返回给 LLM 的结果描述（LLM 需要读懂这段来决定下一步）
        content_dict = {
            "params_used": {"kp": kp_clamped, "ki": ki_clamped, "kd": kd_clamped},
            "clamped_to_bounds": was_clamped,
            "score": result.metrics.score,
            "grade": result.metrics.grade,
            "metrics": {
                "overshoot_pct": result.metrics.overshoot_pct,
                "rise_time_s": result.metrics.rise_time_s,
                "settling_time_s": result.metrics.settling_time_s,
                "sse_pct": result.metrics.sse_pct,
                "oscillation_count": result.metrics.oscillation_count,
                "peak_torque_nm": result.metrics.peak_torque_nm,
            },
            "diagnosis": result.metrics.diagnosis,
            "safety_aborted": result.safety_aborted,
        }
        if result.safety_aborted:
            content_dict["abort_reason"] = result.abort_reason

        return (
            json.dumps(content_dict, ensure_ascii=False),
            result.metrics.score,
            {"kp": kp_clamped, "ki": ki_clamped, "kd": kd_clamped},
        )
