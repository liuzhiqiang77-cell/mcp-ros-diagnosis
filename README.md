# Manastone Diagnostic

**Unitree G1 / Humanoid Robot Operations Agent**  
Part of the [Snakes™](https://github.com/liuzhiqiang77-cell) Agent Platform

---

## Architecture

Manastone is built around **one MCP server per hardware subsystem**. Each server runs on a dedicated port, shares a common DDS bridge and event log, and can be enabled/disabled independently via `config/servers.yaml`.

```
manastone-core    :8080   Diagnosis agent, schema overview, global alerts
manastone-joints  :8081   Joint motor monitoring (temp, torque, velocity, comm)
manastone-power   :8082   Battery voltage, current, SOC, temperature
manastone-imu     :8083   Body posture, tilt detection, fall risk
manastone-hand    :8084   Dexterous hand joints (DEX3, optional)
manastone-vision  :8085   Camera health, depth sensor (M2, stub)
manastone-motion  :8086   Locomotion controller state (M2, stub)
```

Data flow: `ROS2 DDS → DDS Bridge → Schema Engine → SemanticEvent → EventLog → LLM`

---

## Quick Start

### Mock mode (no robot)
```bash
conda create -n manastone python=3.10 -y && conda activate manastone
pip install -e .
export MANASTONE_MOCK_MODE=true
manastone-launcher
```

### Real robot (G1)
```bash
source /opt/ros/humble/setup.bash
conda activate manastone
export MANASTONE_ROBOT_ID=g1_site_01
manastone-launcher
```

### Select which servers to start
```bash
# Edit config/servers.yaml: set enabled: true/false per server
# Or override from command line:
manastone-launcher --enable joints,power,core
manastone-launcher --list   # show all available servers
```

---

## Robot Configuration

All hardware-specific knowledge lives in `config/robot_schema.yaml`:
- **motor_index_map** — maps `motor_state[i]` array index to joint name  
  (sourced from Unitree SDK `G1JointIndex` enum, no hardcoded indices in code)
- **thresholds** — warning/critical values per field, per joint
- **event_types** — semantic event catalog with severity and retention

To add a new robot: run `manastone-launcher --discover` or edit `robot_schema.yaml` following the Unitree G1 reference.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MANASTONE_ROBOT_ID` | `robot_01` | Robot identifier (used in EventLog filename) |
| `MANASTONE_MOCK_MODE` | `false` | `true` = offline test without real DDS |
| `MANASTONE_SCHEMA_PATH` | `config/robot_schema.yaml` | Path to robot schema |
| `MANASTONE_STORAGE_DIR` | `storage` | SQLite EventLog directory |
| `OPENAI_API_KEY` | _(empty)_ | If set, routes LLM calls to cloud |

---

## MCP Tools Reference

### manastone-core (port 8080)
`system_status` · `active_warnings` · `diagnose` · `lookup_fault`  
`schema_overview` · `run_discovery` · `server_registry` · `recent_events` · `event_stats`

### manastone-joints (port 8081)
`joint_status` · `joint_alerts` · `joint_history` · `joint_compare` · `joint_schema`

### manastone-power (port 8082)
`power_status` · `power_alerts` · `power_history` · `charge_estimate`

### manastone-imu (port 8083)
`posture_status` · `posture_alerts` · `posture_history` · `fall_risk`

### manastone-hand (port 8084)
`hand_status` · `hand_alerts` · `hand_history` · `grasp_test`

---

## License

MIT
