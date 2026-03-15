#!/usr/bin/env bash
# Manastone Diagnostic — startup script
set -e

cd "$(dirname "$0")"

# Source ROS2 if available
if [ -f /opt/ros/humble/setup.bash ]; then
    source /opt/ros/humble/setup.bash
    echo "ROS2 Humble sourced"
fi

# Activate conda env if it exists
if command -v conda &>/dev/null && conda env list | grep -q "manastone"; then
    eval "$(conda shell.bash hook)"
    conda activate manastone
fi

# Default to mock mode if no args
export MANASTONE_MOCK_MODE="${MANASTONE_MOCK_MODE:-false}"
export MANASTONE_ROBOT_ID="${MANASTONE_ROBOT_ID:-robot_01}"

echo "Starting Manastone Diagnostic..."
echo "  Robot ID : $MANASTONE_ROBOT_ID"
echo "  Mock mode: $MANASTONE_MOCK_MODE"
echo ""

exec manastone-launcher "$@"
