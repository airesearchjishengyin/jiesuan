#!/bin/bash
# 停止借算本地池 (网关 + 本机节点). 不动 Ollama (其他任务可能共用)。
# 用法: ~/jiesuan/scripts/stop.sh
pkill -f "jiesuan_node --name mac-air" 2>/dev/null && echo "[stop] 节点已停止" || echo "[stop] 节点未在运行"
pkill -f "jiesuan_gateway --mode pool" 2>/dev/null && echo "[stop] 网关已停止" || echo "[stop] 网关未在运行"
