#!/bin/bash
# 查看借算本地池状态
echo "== Ollama (11434) =="
curl -s --max-time 3 http://127.0.0.1:11434/api/tags >/dev/null && echo "✅ 运行中" || echo "❌ 未运行"

echo "== 网关 (7800) =="
if curl -s --max-time 3 -o /dev/null http://127.0.0.1:7800/v1/models; then
  echo "✅ 运行中  控制台: http://127.0.0.1:7800/dashboard"
  curl -s --max-time 3 http://127.0.0.1:7800/v1/models
  echo
else
  echo "❌ 未运行"
fi

echo "== 节点进程 =="
pgrep -fl "jiesuan_node" || echo "❌ 未运行"
pgrep -fl "jiesuan_gateway" || true
