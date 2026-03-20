# PRD — AI 自动 PID 调参系统
## Manastone Diagnostic · PID Auto-Tuning Module

**版本**: 1.0
**日期**: 2026-03-20
**状态**: 已实现（MVP）

---

## 1. 背景与目标

### 1.1 问题陈述

Unitree G1 人形机器人共有 37 个自由度，每个关节独立运行 PID 控制器。传统调参方式依赖经验工程师手动试错，面临以下困难：

| 挑战 | 描述 |
|------|------|
| **评分量化** | 阶跃响应质量（超调/响应速度/稳态误差）难以转化为单一优化目标 |
| **安全约束** | 实验过程中力矩/温度超限会损坏关节电机 |
| **一致性** | 电池电量、环境温度等外部状态影响重复性 |
| **人力成本** | 37 个关节 × N 次迭代 = 不可接受的人工负担 |

### 1.2 产品目标

构建一个基于 **Karpathy autoresearch 架构**的 AI Agent，使 LLM 能够：

1. 自主读取实验文件、分析历史趋势
2. 修改参数文件（YAML），提出有工程依据的调整假设
3. 提交实验（git commit）、运行、评分、决定 keep/discard
4. **循环直到达到目标分数或人工中断**——不达目标不罢休

### 1.3 核心指标

- **目标分数**: 默认 80/100（可配置）
- **安全红线**: 实验期间任何关节力矩 > 60 Nm 立即中止
- **实验上限**: 默认 50 次（安全网，不是目标）

---

## 2. 用户故事

### 主要用户：机器人调试工程师

| ID | 故事 | 验收标准 |
|----|------|----------|
| US-01 | 我想一键启动某关节的自动调参，等它达标后给我最优参数 | `pid_run_research_loop` 返回 `best_params` 且 `target_reached=true` |
| US-02 | 我想在不动真实机器人的情况下验证系统 | `MANASTONE_MOCK_MODE=true` 时使用物理仿真，无需 ROS2 |
| US-03 | 我想查看每次实验的假设和结果，理解 AI 的调参逻辑 | `results.tsv` 和 `git log` 记录完整实验历史 |
| US-04 | 我想确保实验不会损坏硬件 | 三层安全防护：静态边界 + 实验前检查 + 运行时监控 |
| US-05 | 我想随时中断调参并保留当前最优参数 | `best_params.yaml` 实时更新，Ctrl-C 安全中断 |

### 次要用户：LLM Agent / MCP 客户端

| ID | 故事 | 验收标准 |
|----|------|----------|
| US-06 | 我（LLM）想通过 MCP 工具快速验证某组 PID 参数 | `pid_run_experiment` 返回评分和诊断文本 |
| US-07 | 我（LLM）想查询历史最优参数 | `pid_get_best` 返回 kp/ki/kd 及得分 |

---

## 3. 系统架构

### 3.1 整体架构

```
MCP Client (Claude / 工程师)
        │
        │ MCP over SSE (:8087)
        ▼
┌─────────────────────────────────────────────────────────────┐
│  manastone-pid-tuner  (pid_tuner.py)                        │
│                                                             │
│  ┌──────────────┐   ┌───────────────┐   ┌───────────────┐  │
│  │  PIDAgentLoop│   │ExperimentRunner│   │  SafetyGuard  │  │
│  │  agent_loop  │   │  experiment   │   │   safety      │  │
│  └──────┬───────┘   └───────┬───────┘   └───────┬───────┘  │
│         │                   │                   │          │
│  ┌──────▼───────┐   ┌───────▼───────┐           │          │
│  │ PIDWorkspace │   │  PIDScorer    │           │          │
│  │  workspace   │   │   scorer      │           │          │
│  └──────┬───────┘   └───────────────┘           │          │
│         │                                       │          │
│  ┌──────▼──────────────────────────────────────▼────────┐  │
│  │               LLMClient  (llm/client.py)             │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
        │                           │
        ▼                           ▼
 DDS Bridge / Mock           git repo + filesystem
 (ROS2 Joint Data)        storage/pid_workspace/{joint}/
```

### 3.2 autoresearch 核心循环

```
while best_score < target AND exp_count < max_experiments:

    ┌─────────────────────────────────────────────────────┐
    │ Step 1: LLM 读取三个文件                            │
    │   program.md   ← 调参任务说明（人写，不变）         │
    │   params.yaml  ← 当前参数（含上次假设注释）         │
    │   results.tsv  ← 最近15条实验历史                  │
    └────────────────────────┬────────────────────────────┘
                             │ LLM 输出完整 params.yaml 文本
    ┌────────────────────────▼────────────────────────────┐
    │ Step 2: 写入文件 + git commit                       │
    │   write(params.yaml, new_yaml_text)                 │
    │   commit_hash = git commit "exp_0001: Kp=..."       │
    └────────────────────────┬────────────────────────────┘
                             │
    ┌────────────────────────▼────────────────────────────┐
    │ Step 3: 运行实验                                    │
    │   score = run_experiment(kp, ki, kd)                │
    │   → 物理仿真 OR 真实 ROS2 关节                     │
    └────────────────────────┬────────────────────────────┘
                             │
    ┌────────────────────────▼────────────────────────────┐
    │ Step 4: Keep or Discard                             │
    │   if score > best_score:                            │
    │       best_score = score                            │
    │       save_best_params()                            │
    │       status = "keep"                               │
    │   else:                                             │
    │       git checkout HEAD~1 -- params.yaml  ← 回滚   │
    │       status = "discard"                            │
    └────────────────────────┬────────────────────────────┘
                             │
    ┌────────────────────────▼────────────────────────────┐
    │ Step 5: 追加 results.tsv                            │
    │   exp_num | commit_hash | kp | ki | kd |            │
    │   score | grade | overshoot | rise | settle |       │
    │   sse | status | hypothesis                         │
    └─────────────────────────────────────────────────────┘
```

**与旧版本的本质区别**：

| 维度 | 旧版（tool_calls）| 新版（autoresearch）|
|------|------------------|---------------------|
| LLM 角色 | 通过 tool_call(kp=X) 传参数 | 直接编辑 params.yaml 文件 |
| 推理可见性 | 隐藏在 arguments 字段 | 可见于文件注释和 git log |
| 研究日志 | 无结构化日志 | git log = 研究历史 |
| 回滚机制 | 无 | git checkout HEAD~1 |

### 3.3 工作区文件结构

```
storage/pid_workspace/{joint_name}/
├── params.yaml        # LLM 读写（当前参数 + 假设注释）
├── program.md         # 调参任务说明（人写，不变）
├── results.tsv        # 实验日志（追加写入）
└── best_params.yaml   # 历史最优快照（实时更新）
```

**params.yaml 示例**：
```yaml
# PID 调参参数文件 — 由 AI Agent 自动修改
# 假设（Hypothesis）：
#   上次超调18%，Kd从3.0增至5.0以增加阻尼，同时Kp略降至18避免过激
#
# 实验编号：5
# 上一次得分：72.3

joint: left_knee

pid:
  kp: 18.0    # 比例增益
  ki: 0.30    # 积分增益
  kd: 5.0     # 微分增益

experiment:
  setpoint_rad: 0.5
  duration_s: 2.0

safety_bounds:
  kp_range: [1.0, 50.0]
  ki_range: [0.0, 5.0]
  kd_range: [0.0, 20.0]
```

---

## 4. 功能需求

### 4.1 MCP 工具列表

#### F-01: `pid_safety_check`
**输入**: `joint_name: str`
**输出**: `{safe: bool, issues: List[str], battery_soc: float, joint_temp_c: float}`
**描述**: 实验前安全检查（温度、电量、通信状态）

#### F-02: `pid_run_experiment`
**输入**: `joint_name, joint_group, kp, ki, kd, setpoint_rad, duration_s, mock_mode`
**输出**: `{score, grade, overshoot_pct, rise_time_s, settling_time_s, sse_pct, diagnosis}`
**描述**: 运行单次 PID 实验，返回评分和中文诊断

#### F-03: `pid_propose_params`
**输入**: `joint_name, joint_group, current_kp/ki/kd, last_score, history`
**输出**: `{kp, ki, kd, reasoning, strategy}`
**描述**: LLM/规则混合参数建议（单次，不循环）

#### F-04: `pid_run_auto_tuning`
**输入**: `joint_name, joint_group, target_score, max_iterations, ...`
**输出**: `{best_params, best_score, iterations, target_reached, history}`
**描述**: Python for-loop 调参（备用模式，不依赖 LLM 文件编辑）

#### F-05: `pid_run_research_loop` ⭐ 核心工具
**输入**:
```
joint_name: str          # 关节名称
joint_group: str         # 关节组（leg/waist/arm）
target_score: float      # 目标分数（0-100），默认80
max_experiments: int     # 最大实验次数（安全网），默认50
initial_kp/ki/kd: float  # 初始参数
setpoint_rad: float      # 目标角度（rad），默认0.5
experiment_duration_s    # 单次实验时长（s），默认2.0
mock_mode: bool          # 是否使用物理仿真
llm_model: str           # 可选：指定 LLM 模型
```
**输出**:
```json
{
  "joint_name": "left_knee",
  "total_experiments": 12,
  "elapsed_s": 45.3,
  "best_score": 83.7,
  "best_params": {"kp": 18.0, "ki": 0.30, "kd": 5.0},
  "target_reached": true,
  "stopped_by": "target_reached",
  "workspace_dir": "storage/pid_workspace/left_knee",
  "experiment_log": [...]
}
```
**描述**: autoresearch 风格自动调参，LLM 循环直到达标

#### F-06: `pid_get_history`
**输入**: `joint_name: str, last_n: int`
**输出**: `{records: List[ExperimentRecord], total: int}`
**描述**: 查询实验历史记录

#### F-07: `pid_get_best`
**输入**: `joint_name: str`
**输出**: `{kp, ki, kd, score, timestamp}`
**描述**: 获取历史最优参数

#### F-08: `pid_clear_history`
**输入**: `joint_name: str, confirm: bool`
**输出**: `{cleared: bool, message: str}`
**描述**: 清空实验历史（需 confirm=true）

### 4.2 评分系统

评分范围 0-100 分，由五项指标加权扣分：

| 指标 | 满分 | 扣分规则 |
|------|------|---------|
| 超调量 (Overshoot) | 25 | 每超1%扣1分，线性 |
| 上升时间 (Rise Time) | 20 | 超过0.5s开始扣，线性 |
| 调节时间 (Settling) | 25 | 超过1.0s开始扣，线性 |
| 稳态误差 (SSE) | 20 | 每超1%扣2分，线性 |
| 振荡次数 (Oscillation) | 10 | 每次振荡扣3分 |

**评级**：A(≥90) / B(≥75) / C(≥60) / D(≥45) / F(<45)

### 4.3 安全系统

**三层防护**：

```
Layer 1: 静态边界（参数生成时）
  └── kp ∈ [kp_min, kp_max]（来自 robot_schema.yaml）
  └── ki, kd 同上

Layer 2: 实验前检查（每次实验开始前）
  └── 电池 SOC > 20%
  └── 关节温度 < 60°C
  └── 通信丢失次数 == 0

Layer 3: 运行时监控（实验进行中）
  └── |力矩| > 60 Nm → 立即中止
  └── |速度| > 20 rad/s → 立即中止
  └── 温升 > 5°C/实验 → 立即中止
```

**边界配置**（`config/robot_schema.yaml`）：

```yaml
pid_safety_bounds:
  default:    {kp_min: 1.0, kp_max: 50.0, ki_min: 0.0, ki_max: 5.0, kd_min: 0.0, kd_max: 20.0}
  left_knee:  {kp_min: 5.0, kp_max: 40.0, ki_min: 0.0, ki_max: 3.0, kd_min: 0.0, kd_max: 15.0}
  # ... 其他关节
```

---

## 5. 非功能需求

### 5.1 性能
- Mock 模式单次实验 < 100ms（物理仿真 2s 步长 × 10x 加速）
- LLM 调用超时：60s（可配置）
- 每次实验总延迟（仿真 + LLM）< 5s

### 5.2 可靠性
- git 操作失败时静默跳过（不中断调参）
- LLM 输出 YAML 格式错误时跳过本轮并提示 LLM 修正
- 实验异常（仿真发散）时记录 crash，继续下一轮

### 5.3 可观测性
- 每次实验实时 logging（exp_num, kp, ki, kd, score, status）
- `results.tsv` 追加写入，可随时 `tail -f` 监控
- `git log --oneline` 查看完整调参历史

### 5.4 可配置性
- 所有安全边界通过 `config/robot_schema.yaml` 配置
- 服务器启动通过 `config/servers.yaml` 控制（`pid_tuner` 默认关闭）
- LLM 模型/端点通过环境变量配置

---

## 6. 配置与部署

### 6.1 启用 PID 调参服务器

```yaml
# config/servers.yaml
- id: pid_tuner
  port: 8087
  enabled: true   # 改为 true
```

### 6.2 环境变量

```bash
# LLM 配置
export MANASTONE_LLM_REMOTE=true
export MANASTONE_LLM_API_KEY=sk-...
export MANASTONE_LLM_REMOTE_URL=https://api.openai.com/v1
export MANASTONE_LLM_REMOTE_MODEL=gpt-4o

# 或使用本地 LLM
export MANASTONE_LLM_REMOTE=false
export MANASTONE_LLM_LOCAL_URL=http://localhost:8000/v1

# Mock 模式（无需真实机器人）
export MANASTONE_MOCK_MODE=true
```

### 6.3 启动

```bash
# 启动所有服务器（含 pid_tuner）
manastone-launcher

# 仅启动 PID 调参服务器
manastone-pid-tuner

# 快速验证（Mock 模式 + 单工具调用）
python -c "
import asyncio
from manastone_diag.pid_tuning.experiment import ExperimentRunner, ExperimentConfig
runner = ExperimentRunner(mock_mode=True)
cfg = ExperimentConfig('left_knee','leg', kp=10,ki=0.1,kd=2, setpoint_rad=0.5, duration_s=2.0)
result = asyncio.run(runner.run(cfg))
print(f'score={result.metrics.score:.1f}')
"
```

---

## 7. 数据流示意

```
MCP Tool: pid_run_research_loop(joint_name="left_knee", target_score=80)
    │
    ▼
PIDAgentLoop.run()
    │
    ├── workspace.initialize()
    │       └── 写 params.yaml, program.md, results.tsv（首次）
    │
    └── while best < 80 and exp < 50:
            │
            ├── llm.chat(program.md + params.yaml + results.tsv[-15:])
            │       └── 返回新的 params.yaml 文本
            │
            ├── workspace.write_new_params(new_yaml)
            ├── workspace.git_commit("exp_0001: Kp=10.00 ...")
            │
            ├── runner.run(ExperimentConfig(kp, ki, kd))
            │       ├── [Mock] Euler 积分物理仿真
            │       └── [Real] ROS2 DDS 关节指令 + 采样
            │
            ├── scorer.compute_metrics(times, positions, ...)
            │       └── score, grade, overshoot, rise_time, ...
            │
            ├── if score > best:
            │       workspace.save_best(kp, ki, kd, score)
            │   else:
            │       workspace.git_revert_params()  ← git checkout HEAD~1
            │
            └── workspace.log_result(..., status="keep"/"discard")
                    └── 追加一行到 results.tsv
```

---

## 8. 里程碑

### M1（当前 MVP）— 已完成
- [x] `scorer.py` — 五维评分 + 中文诊断
- [x] `safety.py` — 三层安全防护
- [x] `experiment.py` — Mock 物理仿真 + Real 接口
- [x] `workspace.py` — autoresearch 工作区管理（含 git 操作）
- [x] `agent_loop.py` — autoresearch 风格主循环
- [x] `pid_tuner.py` — MCP Server（8 个工具）
- [x] Schema 配置（robot_schema.yaml 安全边界）
- [x] 集成测试通过（Mock 模式）

### M2（计划）
- [ ] 真实 ROS2 关节控制（DDS 写入指令）
- [ ] 多关节并行调参（asyncio.gather）
- [ ] 调参会话断点续传（跨进程 workspace 恢复）
- [ ] Web UI 实时进度展示（Gradio）
- [ ] 贝叶斯优化后备策略（LLM 调用失败时）

### M3（愿景）
- [ ] 全身 37 关节批量调参
- [ ] 跨机器人参数迁移学习
- [ ] 在线调参（不停机，边运动边优化）

---

## 9. 风险与缓解

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|----------|
| LLM 输出无效 YAML | 中 | 低 | 格式验证 + 跳过本轮 + 提示修正 |
| 实验发散（仿真不稳定） | 低 | 低 | 记录 crash，继续下一轮 |
| 真实机器人力矩超限 | 低 | 高 | Layer 3 运行时监控，立即中止 |
| LLM 陷入局部最优 | 中 | 中 | results.tsv 历史可见，规则基备用策略 |
| git 操作失败 | 低 | 低 | 静默跳过，不影响调参流程 |
