#!/bin/bash
# spawn_fleet.sh — 单机起 N 台 mock 异构节点 (逻辑外推压测, 无需真机)
# 用法: scripts/spawn_fleet.sh [N=50] [gateway=127.0.0.1:7800]
# 舰队构成 (i%10): 0-4 small(4b/8GB) · 5 failer(4b/8GB,fail_p=0.25) · 6-8 mid(4b,8b/16GB) · 9 big(4b,8b,14b/32GB)
# 注意: N≤99; 内存估算 ~40MB/节点; 清理用 stop_fleet.sh
set -e
N="${1:-50}"
GW="${2:-127.0.0.1:7800}"
PY="$(cd "$(dirname "$0")/.." && pwd)/.venv/bin/python"
FLEET_DIR="${FLEET_DIR:-/tmp/mock_fleet}"
BASE_PORT="${BASE_PORT:-8100}"
mkdir -p "$FLEET_DIR"
spawned=0
for ((k=0; k<N; k++)); do
  i=$(printf "%02d" "$k")
  name="mock-$i"
  port=$((BASE_PORT + k))
  d=$((k % 10))
  if [ "$d" -le 4 ]; then
    spec="models=qwen3:4b,ram_gb=8,delay=0.3,jitter=0.15"
  elif [ "$d" -eq 5 ]; then
    spec="models=qwen3:4b,ram_gb=8,delay=0.3,jitter=0.2,fail_p=0.25"
  elif [ "$d" -le 8 ]; then
    spec="models=qwen3:4b;qwen3:8b,ram_gb=16,delay=0.2,jitter=0.1"
  else
    spec="models=qwen3:4b;qwen3:8b;qwen3:14b,ram_gb=32,delay=0.1,jitter=0.05"
  fi
  nohup "$PY" -m jiesuan_node --name "$name" --gateway "$GW" --port "$port" \
    --mock --mock-spec "$spec" > "$FLEET_DIR/$name.log" 2>&1 &
  spawned=$((spawned + 1))
done
echo "spawned $spawned mock nodes → $GW (ports ${BASE_PORT}.., logs: $FLEET_DIR/)"
echo "等 12s 后用 scripts/analyze_trace.py 或 curl $GW/nodes 查看注册"
