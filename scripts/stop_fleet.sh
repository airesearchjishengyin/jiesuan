#!/bin/bash
# stop_fleet.sh — 停掉全部 mock 节点 (按 --mock 标志匹配, 不影响真实节点如 mac-air)
pkill -f "jiesuan_node.*--mock" 2>/dev/null && echo "[fleet] mock 节点已全部停止" \
  || echo "[fleet] 无运行中的 mock 节点"
