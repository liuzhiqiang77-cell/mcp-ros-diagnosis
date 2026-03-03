# Manastone Diagnostic — 部署与使用手册

**版本**: v0.1 | **目标平台**: Unitree G1 · Jetson Orin NX (192.168.123.164)

---

## 目录

1. [系统概述](#1-系统概述)
2. [当前能力边界](#2-当前能力边界)
3. [硬件与网络要求](#3-硬件与网络要求)
4. [方案A：部署在开发机 Mac（推荐用于调试）](#4-方案a开发机-mac-部署)
5. [方案B：部署在机器人 Orin NX（生产部署）](#5-方案b机器人-orin-nx-部署)
6. [工程师使用指南](#6-工程师使用指南)
7. [故障诊断知识库说明](#7-故障诊断知识库说明)
8. [常见问题排查](#8-常见问题排查)

---

## 1. 系统概述

Manastone Diagnostic 是一个运行在 G1 机器人 Orin NX 上的语义化诊断工具，为现场工程师提供自然语言交互的故障分析能力。

```
工程师浏览器
     │
     │ HTTP :7860
     ▼
┌─────────────────────────────────────────┐
│          Manastone Diagnostic           │
│                                         │
│  Gradio Web UI (:7860)                  │
│       │                                 │
│  DiagnosticOrchestrator                 │
│       ├─ DDS Bridge (订阅机器人数据)    │
│       ├─ fault_library.yaml (知识库)    │
│       └─ LLM Client                     │
│              ├─ 云端 Kimi (有网时)      │
│              └─ 本地 Qwen2.5-7B (离线) │
└─────────────────────────────────────────┘
     │
     │ DDS Domain 0
     ▼
G1 RockChip (192.168.123.161)  ← 运动控制器，只读，不写入
```

---

## 2. 当前能力边界

> **部署前必读**

| 功能 | 状态 | 说明 |
|------|------|------|
| Web UI 智能对话 | ✅ 可用 | 需要 LLM（云端或本地） |
| 快速诊断（温度/对比） | ✅ 可用 | |
| Mock 故障场景模拟 | ✅ 可用 | 用于测试和培训 |
| MCP Server (SSE :8080) | ✅ 可用 | 供外部 AI Agent 调用 |
| **真实 DDS 数据接入** | ⚠️ 开发中 | 当前自动降级为 Mock 模式 |
| 本地 Qwen2.5-7B | ⚠️ 未安装 | 需单独下载部署 |
| 写入/控制机器人 | 🚫 不支持 | 纯只读诊断 |

**结论**：当前版本可在机器人上部署并运行，但机器人关节数据来自模拟生成，不是真实传感器数据。真实 DDS 接入待下一版本完成。

---

## 3. 硬件与网络要求

### 网络拓扑

```
开发机 Mac (Wi-Fi)
   └── 192.168.123.x 网段
         ├── 192.168.123.164  Orin NX（部署目标）
         └── 192.168.123.161  RockChip（运动控制器，只读）
```

### Orin NX 最低要求

| 项目 | 要求 |
|------|------|
| OS | Ubuntu 20.04 / JetPack 5.x |
| Python | 3.10 或以上 |
| 内存 | 可用 ≥ 2GB（无本地 LLM）/ ≥ 10GB（含 Qwen2.5-7B） |
| 磁盘 | 可用 ≥ 2GB（无本地 LLM）/ ≥ 20GB（含模型） |
| 网络 | 与 192.168.123.161 同网段 |

---

## 4. 方案A：开发机 Mac 部署

适用于**调试、演示、培训**，不需要机器人硬件。

### 4.1 安装

```bash
cd ~/manastone-diagnostic
pip install -e ".[dev]"
```

### 4.2 配置 .env（使用云端 Kimi）

```bash
# ~/manastone-diagnostic/.env
OPENAI_API_KEY=sk-kimi-xxxxxxxxxxxxxxxx
OPENAI_API_BASE=https://api.kimi.com/coding/v1
LLM_MODEL=kimi-for-coding
```

### 4.3 启动（Mock 模式）

```bash
cd ~/manastone-diagnostic

# 启动 Web UI（后台运行）
MANASTONE_MOCK_MODE=true manastone-ui &

# 或同时启动 MCP Server
MANASTONE_MOCK_MODE=true manastone-diag &
```

### 4.4 访问

打开浏览器访问 `http://localhost:7860`

---

## 5. 方案B：机器人 Orin NX 部署

### 5.1 从 Mac 推送代码到机器人

```bash
# 在 Mac 上执行
rsync -avz --exclude='.venv' --exclude='__pycache__' --exclude='.git' \
  ~/manastone-diagnostic/ \
  unitree@192.168.123.164:~/manastone-diagnostic/
```

> 如果没有配置 SSH 密钥，先执行：
> ```bash
> ssh-copy-id unitree@192.168.123.164
> ```

### 5.2 登录 Orin NX

```bash
ssh unitree@192.168.123.164
```

### 5.3 安装 Python 依赖

```bash
cd ~/manastone-diagnostic

# 创建虚拟环境（推荐）
python3 -m venv .venv
source .venv/bin/activate

# 安装包
pip install -e "."
```

### 5.4 配置 LLM

**选项 1：使用云端 Kimi（需要 Orin NX 能访问公网）**

```bash
cat > ~/manastone-diagnostic/.env << 'EOF'
OPENAI_API_KEY=sk-kimi-xxxxxxxxxxxxxxxx
OPENAI_API_BASE=https://api.kimi.com/coding/v1
LLM_MODEL=kimi-for-coding
EOF
```

**选项 2：使用本地 Qwen2.5-7B（完全离线）**

需要先下载并运行本地推理服务：

```bash
# 安装 llama.cpp 或 vLLM（二选一）
# 示例：llama.cpp
pip install llama-cpp-python

# 下载模型（约 4.5GB，GGUF 格式）
# 模型放到 ~/manastone-diagnostic/models/

# 启动本地推理服务（在后台保持运行）
python3 -m llama_cpp.server \
  --model ~/manastone-diagnostic/models/qwen2.5-7b-instruct-q4_k_m.gguf \
  --host 127.0.0.1 \
  --port 8081 \
  --n_ctx 4096 &

# .env 保持空（无 OPENAI_API_KEY）则自动使用本地
```

> 本地模型推理速度在 Orin NX 上约 10-20 token/s，响应时间约 15-30 秒。

### 5.5 创建 systemd 服务（开机自启）

```bash
sudo tee /etc/systemd/system/manastone-ui.service > /dev/null << 'EOF'
[Unit]
Description=Manastone Diagnostic Web UI
After=network.target

[Service]
Type=simple
User=unitree
WorkingDirectory=/home/unitree/manastone-diagnostic
EnvironmentFile=/home/unitree/manastone-diagnostic/.env
Environment=MANASTONE_MOCK_MODE=true
ExecStart=/home/unitree/manastone-diagnostic/.venv/bin/manastone-ui
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable manastone-ui
sudo systemctl start manastone-ui
```

### 5.6 验证服务状态

```bash
# 检查服务是否运行
sudo systemctl status manastone-ui

# 查看实时日志
journalctl -u manastone-ui -f

# 检查端口
ss -tlnp | grep 7860
```

### 5.7 从 Mac 访问 UI

在 Mac 浏览器访问：`http://192.168.123.164:7860`

---

## 6. 工程师使用指南

### 6.1 打开界面

浏览器访问：
- 本地：`http://localhost:7860`
- 机器人上：`http://192.168.123.164:7860`

---

### 6.2 标签页功能说明

#### 💬 智能诊断（主要使用场景）

与 AI 对话描述故障现象，系统自动结合机器人实时状态和知识库给出诊断。

**使用方法**：

在输入框中用自然语言描述问题，点击"发送"。

**推荐提问方式**：

| 场景 | 输入示例 |
|------|----------|
| 关节异常 | `左腿膝关节发烫，停机了` |
| 步态问题 | `走路一直往右边偏` |
| 传感器故障 | `摄像头无法初始化，深度图没有` |
| 维护询问 | `关节过热应该怎么处理？` |
| 定期维护 | `运行了 200 小时，需要做什么维护？` |

**快捷示例**：点击页面下方的示例按钮可快速填入常见问题。

---

#### 📊 实时状态

点击"刷新数据"查看所有关节的当前温度、扭矩、位置数据（JSON 格式）。

---

#### 🔍 快速诊断

三个一键诊断按钮：

| 按钮 | 功能 |
|------|------|
| 🌡️ 温度诊断 | 检查所有关节温度，标出超温关节 |
| ⚖️ 左右对比 | 对比左右对称关节，发现单侧异常 |
| 🔎 全面诊断 | 综合所有指标给出健康报告 |

---

#### 🎮 场景模拟

> 仅 Mock 模式下有效，用于**培训和演示**。

选择一个故障场景，系统会模拟对应的机器人数据，可用来训练工程师识别各类故障特征。

| 场景 | 用途 |
|------|------|
| 正常站立 / 正常行走 | 基准参考 |
| 左膝过热 | 演示单关节过热诊断流程 |
| 编码器故障 | 演示位置数据异常特征 |
| 左右不对称 | 演示机械磨损/代偿步态 |

---

### 6.3 典型诊断流程

```
1. 机器人出现异常
       │
       ▼
2. 打开浏览器 → http://192.168.123.164:7860
       │
       ▼
3. 【快速诊断】标签 → 点击"全面诊断"
   确认是否有异常关节和报警
       │
       ├─ 有异常 ──► 4a. 【智能诊断】描述现象
       │                   AI 给出根因分析 + 处理步骤
       │
       └─ 无异常 ──► 4b. 可能是间歇性问题
                        在【实时状态】观察 1-2 分钟数据变化
       │
       ▼
5. 根据 AI 建议执行处理
   （立即处理 / 短期检查 / 长期维护）
       │
       ▼
6. 处理后再次运行"全面诊断"确认恢复正常
```

---

## 7. 故障诊断知识库说明

知识库位于 `knowledge/fault_library.yaml`，包含以下故障分类：

| 故障码 | 名称 | 严重级别 |
|--------|------|----------|
| FK-001 | 关节编码器通信异常 | CRITICAL |
| FK-002 | 关节电机过流保护 | CRITICAL |
| FK-003 | 关节过热保护 | WARNING |
| FK-004 | LiDAR 点云稀疏/缺失 | WARNING |
| FK-005 | RealSense 初始化失败 | WARNING |
| FK-006 | IMU 数据漂移 | NOTICE |
| FK-007 | 关节位置跟踪误差偏大 | NOTICE |
| FK-008 | 灵巧手通信断连 | WARNING |

**温度阈值**：
- > 50°C：WARNING（黄色警告）
- > 65°C：CRITICAL（红色危险，立即停机）

**添加新故障**：直接编辑 `knowledge/fault_library.yaml`，按现有格式添加条目，无需重启服务（下次查询时自动加载）。

---

## 8. 常见问题排查

### UI 无法访问

```bash
# 检查进程
ps aux | grep manastone

# 检查端口
ss -tlnp | grep 7860

# 手动重启
pkill -f manastone-ui
cd ~/manastone-diagnostic
MANASTONE_MOCK_MODE=true .venv/bin/manastone-ui &
```

### UI 显示使用本地 Qwen 而非云端 Kimi

原因：`.env` 文件未被加载，`OPENAI_API_KEY` 为空。

```bash
# 检查 .env 是否存在且有内容
cat ~/manastone-diagnostic/.env

# 检查环境变量是否正确
cd ~/manastone-diagnostic
.venv/bin/python3 -c "
from src.manastone_diag.config import get_config
c = get_config()
print('use_remote:', c.llm.use_remote)
print('model:', c.llm.remote_model if c.llm.use_remote else c.llm.local_model)
"
```

### AI 对话无响应或超时

```bash
# 检查 Kimi API 连通性
curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  https://api.kimi.com/coding/v1/models

# 检查本地 LLM（如使用）
curl http://127.0.0.1:8081/v1/models
```

### 更新代码后重新部署

```bash
# 在 Mac 上
cd ~/manastone-diagnostic
git pull

rsync -avz --exclude='.venv' --exclude='__pycache__' --exclude='.git' \
  ~/manastone-diagnostic/ \
  unitree@192.168.123.164:~/manastone-diagnostic/

# 在 Orin NX 上
ssh unitree@192.168.123.164
sudo systemctl restart manastone-ui
```

---

## 附录：端口一览

| 服务 | 端口 | 用途 |
|------|------|------|
| Web UI | 7860 | 工程师浏览器访问 |
| MCP Server | 8080 | 外部 AI Agent 调用（SSE） |
| 本地 LLM | 8081 | Qwen2.5-7B 推理（可选） |

## 附录：关节 ID 映射

| ID | 关节名 | ID | 关节名 |
|----|--------|----|--------|
| 0 | L_HIP_ROLL | 6 | R_HIP_ROLL |
| 1 | L_HIP_YAW | 7 | R_HIP_YAW |
| 2 | L_HIP_PITCH | 8 | R_HIP_PITCH |
| 3 | L_KNEE | 9 | R_KNEE |
| 4 | L_ANKLE_PITCH | 10 | R_ANKLE_PITCH |
| 5 | L_ANKLE_ROLL | 11 | R_ANKLE_ROLL |
| 12 | WAIST_YAW | — | — |
| 13 | L_SHOULDER_PITCH | 18 | R_SHOULDER_PITCH |
| 14 | L_SHOULDER_ROLL | 19 | R_SHOULDER_ROLL |
| 15 | L_SHOULDER_YAW | 20 | R_SHOULDER_YAW |
| 16 | L_ELBOW | 21 | R_ELBOW |
| 17 | L_WRIST | 22 | R_WRIST |
