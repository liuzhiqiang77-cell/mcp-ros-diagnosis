"""
G1 机器人 Mock 数据场景引擎

设计原则：
1. 物理联动 —— 扭矩²驱动热功率，一阶热模型积分温度
2. 真实关节范围 —— G1 EDU 各关节物理限位和额定扭矩
3. 步态仿真 —— 双足行走/站立的周期性关节轨迹
4. 场景模式 —— 8种可切换场景，每种有独立的故障注入逻辑
5. 渐进式故障 —— 温度/误差随时间积累，不是瞬间跳变
"""

import math
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

# ─────────────────────────────────────────────────────────
# G1 关节物理参数
# (pos_min_rad, pos_max_rad, rated_torque_Nm, thermal_resistance)
# thermal_resistance：越大越容易热（小关节电机散热差）
# ─────────────────────────────────────────────────────────
JOINT_PROPS: dict[int, tuple] = {
    # 腿部（承重，额定扭矩大）
    0:  (-0.52,  0.52,   88, 1.2),   # 左髋偏航
    1:  (-0.87,  2.09,  120, 1.0),   # 左髋俯仰
    2:  (-0.52,  0.52,   88, 1.2),   # 左髋滚转
    3:  ( 0.00,  2.70,  139, 0.9),   # 左膝（最大额定扭矩）
    4:  (-0.87,  0.87,   50, 1.5),   # 左踝俯仰
    5:  (-0.26,  0.26,   50, 1.8),   # 左踝滚转
    6:  (-0.52,  0.52,   88, 1.2),   # 右髋偏航
    7:  (-0.87,  2.09,  120, 1.0),   # 右髋俯仰
    8:  (-0.52,  0.52,   88, 1.2),   # 右髋滚转
    9:  ( 0.00,  2.70,  139, 0.9),   # 右膝
    10: (-0.87,  0.87,   50, 1.5),   # 右踝俯仰
    11: (-0.26,  0.26,   50, 1.8),   # 右踝滚转
    # 躯干
    12: (-2.09,  2.09,   80, 1.1),   # 腰偏航
    13: (-0.52,  1.57,   80, 1.1),   # 腰俯仰
    # 左臂
    14: (-2.70,  2.70,   45, 2.0),   # 左肩偏航
    15: (-2.87,  2.87,   45, 2.0),   # 左肩俯仰
    16: (-1.57,  1.57,   45, 2.0),   # 左肩滚转
    17: (-1.57,  2.62,   45, 2.0),   # 左肘
    18: (-1.57,  1.57,   18, 3.0),   # 左腕俯仰
    19: (-1.57,  1.57,   18, 3.0),   # 左腕滚转
    # 右臂
    20: (-2.70,  2.70,   45, 2.0),   # 右肩偏航
    21: (-2.87,  2.87,   45, 2.0),   # 右肩俯仰
    22: (-1.57,  1.57,   45, 2.0),   # 右肩滚转
    23: (-1.57,  2.62,   45, 2.0),   # 右肘
    24: (-1.57,  1.57,   18, 3.0),   # 右腕俯仰
    25: (-1.57,  1.57,   18, 3.0),   # 右腕滚转
    # 颈部
    26: (-1.04,  1.04,   10, 4.0),   # 颈偏航
    27: (-0.87,  0.87,   10, 4.0),   # 颈俯仰
    28: (-0.52,  0.52,   10, 4.0),   # 颈滚转
}

T_AMBIENT   = 25.0    # 环境温度 °C
T_INIT_BASE = 33.0    # 启动初始温度
DT          = 0.5     # 仿真步长 s（2 Hz）
TAU_HEAT    = 150.0   # 热时间常数 s（约 2.5 分钟升到 63%）
POWER_COEFF = 0.0018  # 热功率系数 (°C·s⁻¹ per Nm²)


# ─────────────────────────────────────────────────────────
# 场景枚举
# ─────────────────────────────────────────────────────────
class ScenarioType(str, Enum):
    NORMAL_IDLE        = "normal_idle"        # 静止站立
    NORMAL_WALKING     = "normal_walking"     # 正常行走
    OVERHEAT_LEFT_KNEE = "overheat_left_knee" # 左膝过热
    OVERHEAT_BILATERAL = "overheat_bilateral" # 双膝过热（长时间行走）
    ENCODER_FAULT      = "encoder_fault"      # 左髋编码器故障
    ASYMMETRY          = "asymmetry"          # 左右不对称（右腿代偿）
    CARRYING_LOAD      = "carrying_load"      # 持重物（上肢高负载）
    JOINT_STIFFNESS    = "joint_stiffness"    # 右踝关节发僵

SCENARIO_DESCRIPTIONS = {
    ScenarioType.NORMAL_IDLE:        "正常站立：所有关节温度正常，低扭矩",
    ScenarioType.NORMAL_WALKING:     "正常行走：双腿周期运动，温度缓慢上升",
    ScenarioType.OVERHEAT_LEFT_KNEE: "左膝过热：持续高负载导致左膝温度超阈值",
    ScenarioType.OVERHEAT_BILATERAL: "双膝过热：长时间行走后双侧膝关节过热",
    ScenarioType.ENCODER_FAULT:      "左髋编码器故障：位置反馈异常跳变",
    ScenarioType.ASYMMETRY:          "左右不对称：右腿代偿，左右温度/扭矩差异显著",
    ScenarioType.CARRYING_LOAD:      "持重物：双臂和腰部高扭矩，上肢温度上升",
    ScenarioType.JOINT_STIFFNESS:    "右踝发僵：高扭矩低速度，温度快速上升",
}


# ─────────────────────────────────────────────────────────
# 关节热模型
# ─────────────────────────────────────────────────────────
class JointThermalState:
    """一阶 RC 热模型：dT/dt = P_heat/C - (T - T_env)/τ"""

    def __init__(self, joint_id: int):
        self.joint_id = joint_id
        _, _, _, thermal_res = JOINT_PROPS[joint_id]
        self.thermal_res = thermal_res
        self.temperature = T_INIT_BASE + random.uniform(-1.5, 1.5)

    def update(self, torque_nm: float, power_multiplier: float = 1.0) -> float:
        p_heat = POWER_COEFF * torque_nm ** 2 * self.thermal_res * power_multiplier
        cooling = (self.temperature - T_AMBIENT) / TAU_HEAT
        self.temperature += DT * (p_heat - cooling)
        self.temperature += random.gauss(0, 0.08)  # 传感器噪声
        return self.temperature

    def force_set(self, target: float, rate: float = 0.05):
        """外部强制拉向目标温度（用于场景切换时快速初始化）"""
        self.temperature += (target - self.temperature) * rate


# ─────────────────────────────────────────────────────────
# 步态生成器
# ─────────────────────────────────────────────────────────
class GaitGenerator:
    """
    简化双足行走步态（矢状面）
    使用正弦曲线近似髋关节、膝关节、踝关节的周期运动
    步频 ~1 Hz，步幅中等
    """
    STRIDE_FREQ = 0.8   # Hz，步频
    HIP_PITCH_AMP = 0.35   # rad，髋关节俯仰幅度
    KNEE_AMP       = 0.55   # rad，膝关节弯曲幅度（0到peak）
    ANKLE_AMP      = 0.25   # rad，踝关节幅度
    HIP_ROLL_AMP   = 0.10   # rad，侧向摆动
    ARM_SWING_AMP  = 0.30   # rad，手臂摆动

    def joint_targets(self, t: float) -> dict[int, tuple[float, float, float]]:
        """
        返回各关节的 (position, velocity, torque_nominal)
        左右腿相位差 π（对步）
        """
        w = 2 * math.pi * self.STRIDE_FREQ
        phi_l = w * t           # 左腿相位
        phi_r = phi_l + math.pi  # 右腿（半周期差）

        targets: dict[int, tuple[float, float, float]] = {}

        # ── 左腿 ──
        hip_p_l = self.HIP_PITCH_AMP * math.sin(phi_l)
        knee_l  = self.KNEE_AMP * max(0, math.sin(phi_l + 0.4))   # 膝只弯不伸过头
        ankle_l = -self.ANKLE_AMP * math.sin(phi_l - 0.3)
        hip_r_l = self.HIP_ROLL_AMP * math.sin(phi_l + math.pi / 2)
        targets[0] = (0.0,  0.0,  8.0)                           # 左髋偏航
        targets[1] = (hip_p_l, w * self.HIP_PITCH_AMP * math.cos(phi_l), 45.0 + abs(hip_p_l) * 30)
        targets[2] = (hip_r_l, 0.0, 15.0)                        # 左髋滚转
        targets[3] = (knee_l,  w * self.KNEE_AMP * max(0, math.cos(phi_l + 0.4)), 55.0 + knee_l * 25)
        targets[4] = (ankle_l, w * (-self.ANKLE_AMP) * math.cos(phi_l - 0.3), 22.0)
        targets[5] = (0.0,  0.0,  8.0)                           # 左踝滚转

        # ── 右腿 ──
        hip_p_r = self.HIP_PITCH_AMP * math.sin(phi_r)
        knee_r  = self.KNEE_AMP * max(0, math.sin(phi_r + 0.4))
        ankle_r = -self.ANKLE_AMP * math.sin(phi_r - 0.3)
        hip_r_r = self.HIP_ROLL_AMP * math.sin(phi_r + math.pi / 2)
        targets[6] = (0.0,  0.0,  8.0)
        targets[7] = (hip_p_r, w * self.HIP_PITCH_AMP * math.cos(phi_r), 45.0 + abs(hip_p_r) * 30)
        targets[8] = (hip_r_r, 0.0, 15.0)
        targets[9] = (knee_r,  w * self.KNEE_AMP * max(0, math.cos(phi_r + 0.4)), 55.0 + knee_r * 25)
        targets[10] = (ankle_r, w * (-self.ANKLE_AMP) * math.cos(phi_r - 0.3), 22.0)
        targets[11] = (0.0, 0.0, 8.0)

        # ── 躯干 ──
        targets[12] = (0.0, 0.0, 5.0)   # 腰偏航
        targets[13] = (0.05, 0.0, 20.0) # 腰俯仰（微前倾）

        # ── 手臂（与腿对侧摆动）──
        arm_l = self.ARM_SWING_AMP * math.sin(phi_r)  # 左臂与右腿同相
        arm_r = self.ARM_SWING_AMP * math.sin(phi_l)
        targets[14] = (0.0, 0.0, 5.0)
        targets[15] = (arm_l, w * self.ARM_SWING_AMP * math.cos(phi_r), 12.0)
        targets[16] = (0.0, 0.0, 5.0)
        targets[17] = (0.3, 0.0, 8.0)   # 肘微弯
        targets[18] = (0.0, 0.0, 3.0)
        targets[19] = (0.0, 0.0, 3.0)
        targets[20] = (0.0, 0.0, 5.0)
        targets[21] = (arm_r, w * self.ARM_SWING_AMP * math.cos(phi_l), 12.0)
        targets[22] = (0.0, 0.0, 5.0)
        targets[23] = (0.3, 0.0, 8.0)
        targets[24] = (0.0, 0.0, 3.0)
        targets[25] = (0.0, 0.0, 3.0)

        # ── 颈部 ──
        targets[26] = (0.0, 0.0, 2.0)
        targets[27] = (0.1, 0.0, 2.0)   # 微低头
        targets[28] = (0.0, 0.0, 1.0)

        return targets


def _idle_targets() -> dict[int, tuple[float, float, float]]:
    """静止站立姿态目标（pos, vel, torque）"""
    return {
        0: (0.0,  0.0,  3.0),  # 左髋偏航
        1: (0.1,  0.0, 18.0),  # 左髋俯仰（微前倾）
        2: (0.0,  0.0,  5.0),  # 左髋滚转
        3: (0.3,  0.0, 28.0),  # 左膝（微弯，承重）
        4: (-0.1, 0.0, 12.0),  # 左踝俯仰
        5: (0.0,  0.0,  4.0),  # 左踝滚转
        6: (0.0,  0.0,  3.0),
        7: (0.1,  0.0, 18.0),
        8: (0.0,  0.0,  5.0),
        9: (0.3,  0.0, 28.0),
        10: (-0.1, 0.0, 12.0),
        11: (0.0,  0.0,  4.0),
        12: (0.0,  0.0,  8.0),  # 腰
        13: (0.05, 0.0, 22.0),
        **{i: (0.0, 0.0, 3.0) for i in range(14, 29)},  # 上肢/颈放松
    }


# ─────────────────────────────────────────────────────────
# 场景引擎
# ─────────────────────────────────────────────────────────
class ScenarioEngine:
    """
    驱动所有场景，维护 29 个关节的热状态，
    每次调用 step() 返回一帧 motor_state。
    """

    def __init__(self):
        self.thermal = {i: JointThermalState(i) for i in range(29)}
        self.gait = GaitGenerator()
        self._t = 0.0                          # 仿真时间
        self._scenario = ScenarioType.NORMAL_WALKING
        self._scenario_start = 0.0            # 场景开始时间（用于渐进故障）
        self._encoder_fault_phase = 0.0       # 编码器故障随机状态

    @property
    def scenario(self) -> ScenarioType:
        return self._scenario

    @scenario.setter
    def scenario(self, s: ScenarioType):
        if s != self._scenario:
            self._scenario = s
            self._scenario_start = self._t
            # 切换场景时不重置温度（保留热历史，更真实）

    def step(self) -> list[dict]:
        """推进一个时间步，返回 29 个关节的状态字典列表"""
        self._t += DT
        elapsed = self._t - self._scenario_start

        # 1. 获取基础关节目标
        if self._scenario == ScenarioType.NORMAL_IDLE:
            targets = _idle_targets()
            power_mults = {i: 0.3 for i in range(29)}  # 低功率

        elif self._scenario == ScenarioType.NORMAL_WALKING:
            targets = self.gait.joint_targets(self._t)
            power_mults = {i: 1.0 for i in range(29)}

        elif self._scenario == ScenarioType.OVERHEAT_LEFT_KNEE:
            targets = self.gait.joint_targets(self._t)
            power_mults = {i: 1.0 for i in range(29)}
            # 左膝：散热受阻（润滑脂老化），等效功率×3.5，模拟缓慢过热
            power_mults[3] = 3.5
            # 扭矩也偏高
            pos, vel, torq = targets[3]
            targets[3] = (pos, vel, torq * 1.4)

        elif self._scenario == ScenarioType.OVERHEAT_BILATERAL:
            targets = self.gait.joint_targets(self._t)
            power_mults = {i: 1.0 for i in range(29)}
            # 双膝持续高功率（长时间行走积热）
            power_mults[3] = 2.8   # 左膝
            power_mults[9] = 2.5   # 右膝（稍轻）
            pos3, vel3, t3 = targets[3]
            pos9, vel9, t9 = targets[9]
            targets[3] = (pos3, vel3, t3 * 1.3)
            targets[9] = (pos9, vel9, t9 * 1.3)

        elif self._scenario == ScenarioType.ENCODER_FAULT:
            targets = self.gait.joint_targets(self._t)
            power_mults = {i: 1.0 for i in range(29)}
            # 左髋偏航（关节0）：编码器故障，位置读数随机跳变
            # 故障从第10秒开始，之前表现正常（让用户看到对比）
            if elapsed > 10.0:
                pos, vel, torq = targets[0]
                # 模拟编码器丢步/乱码
                fault_magnitude = min((elapsed - 10.0) / 30.0, 1.0)
                if random.random() < 0.3 * fault_magnitude:
                    # 随机跳变到错误位置
                    pos = random.uniform(-1.5, 1.5)   # 超出物理范围
                    vel = random.uniform(-5.0, 5.0)   # 速度异常
                targets[0] = (pos, vel, torq)

        elif self._scenario == ScenarioType.ASYMMETRY:
            targets = self.gait.joint_targets(self._t)
            power_mults = {i: 1.0 for i in range(29)}
            # 左腿（0-5）：减速器磨损，摩擦大，扭矩 ×1.6，功率 ×2.2
            for jid in [1, 2, 3, 4]:
                pos, vel, torq = targets[jid]
                targets[jid] = (pos, vel, torq * 1.6)
                power_mults[jid] = 2.2
            # 右腿代偿：右腿额外负荷
            for jid in [7, 8, 9, 10]:
                pos, vel, torq = targets[jid]
                targets[jid] = (pos, vel, torq * 1.15)
                power_mults[jid] = 1.3

        elif self._scenario == ScenarioType.CARRYING_LOAD:
            targets = _idle_targets()
            power_mults = {i: 0.4 for i in range(29)}
            # 站立 + 双臂前举持重（~5 kg 哑铃）
            load_torque = 35.0  # Nm
            # 左臂：前举姿态
            targets[14] = (0.0,  0.0, 5.0)
            targets[15] = (1.2,  0.0, load_torque)   # 左肩俯仰（前举）
            targets[16] = (0.0,  0.0, 8.0)
            targets[17] = (0.8,  0.0, load_torque * 0.7)  # 左肘微弯
            targets[18] = (0.0,  0.0, 5.0)
            targets[19] = (0.0,  0.0, 5.0)
            # 右臂：同姿态
            targets[20] = (0.0,  0.0, 5.0)
            targets[21] = (1.2,  0.0, load_torque)
            targets[22] = (0.0,  0.0, 8.0)
            targets[23] = (0.8,  0.0, load_torque * 0.7)
            targets[24] = (0.0,  0.0, 5.0)
            targets[25] = (0.0,  0.0, 5.0)
            # 腰前倾补偿
            targets[13] = (0.3, 0.0, 45.0)
            # 臂关节功率×4
            for jid in [14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25]:
                power_mults[jid] = 4.0
            power_mults[13] = 2.0  # 腰

        elif self._scenario == ScenarioType.JOINT_STIFFNESS:
            targets = self.gait.joint_targets(self._t)
            power_mults = {i: 1.0 for i in range(29)}
            # 右踝俯仰（关节10）：关节发僵，高扭矩但运动受限
            pos, vel, torq = targets[10]
            # 位置被限制在小范围
            stiffness_factor = min(elapsed / 20.0, 1.0)  # 渐进发展
            limited_pos = pos * (1.0 - 0.7 * stiffness_factor)
            high_torq = torq * (1.0 + 2.5 * stiffness_factor)
            targets[10] = (limited_pos, vel * 0.3, high_torq)
            power_mults[10] = 1.0 + 4.0 * stiffness_factor  # 功率急剧上升

        else:
            targets = _idle_targets()
            power_mults = {i: 0.3 for i in range(29)}

        # 2. 生成各关节状态，加传感器噪声，更新热模型
        result = []
        for jid in range(29):
            pos_min, pos_max, max_torq, _ = JOINT_PROPS[jid]
            tgt_pos, tgt_vel, tgt_torq = targets.get(jid, (0.0, 0.0, 5.0))

            # 位置：目标 + 跟踪误差噪声（±0.5%满量程）
            range_span = pos_max - pos_min
            pos_noise = random.gauss(0, range_span * 0.005)
            pos = float(tgt_pos) + pos_noise

            # 速度：目标 + 噪声
            vel = float(tgt_vel) + random.gauss(0, 0.02)

            # 扭矩：目标 + 噪声 + 限幅
            torq = float(tgt_torq) + random.gauss(0, tgt_torq * 0.05 + 0.5)
            torq = max(-max_torq, min(max_torq, torq))

            # 热模型更新
            temp = self.thermal[jid].update(torq, power_mults.get(jid, 1.0))

            result.append({
                "joint_id":   jid,
                "position":   pos,
                "velocity":   vel,
                "torque":     torq,
                "temperature": temp,
            })

        return result
