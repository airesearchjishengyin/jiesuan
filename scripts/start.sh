#!/bin/bash
# 启动借算本地池 (网关 + 本机节点 + 依赖 Ollama)
# 用法: ~/jiesuan/scripts/start.sh
set -e
cd "$(dirname "$0")/.."
PY="$PWD/.venv/bin/python"

# 公网鉴权 token (存在 .env 则加载: JIESUAN_NODE_TOKEN / JIESUAN_API_KEY)
if [ -f .env ]; then
  # 只加载合法 KEY=VALUE 行 (坏行/注释自动跳过, 不因手误卡死启动)
  set -a; eval "$(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' .env)"; set +a
fi

# 1. Ollama (节点的推理引擎)
if ! curl -s --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null; then
  echo "[start] 启动 Ollama ..."
  # 16GB 机器内存治理: 最多驻留1个模型 (防多模型堆叠→swap风暴) + 空闲1小时卸载
  launchctl setenv OLLAMA_MAX_LOADED_MODELS 1 2>/dev/null || true
  launchctl setenv OLLAMA_KEEP_ALIVE 1h 2>/dev/null || true
  open -a Ollama 2>/dev/null || ollama serve >/dev/null 2>&1 &
  for i in $(seq 1 15); do
    curl -s --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null && break
    sleep 1
  done
fi
curl -s --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null || { echo "[start] Ollama 启动失败"; exit 1; }

# 1.5 预热默认模型 (冷加载 1-2min 会让 Hermes 流式首字节超时报 empty stream)
WARMUP_MODEL="${WARMUP_MODEL:-qwen3.5:9b}"
if ! curl -s --max-time 3 http://127.0.0.1:11434/api/ps | grep -q "$WARMUP_MODEL"; then
  echo "[start] 预热模型 $WARMUP_MODEL (保持驻留, 首次约 1-2 分钟)..."
  curl -s --max-time 300 http://127.0.0.1:11434/api/generate \
    -d "{\"model\":\"$WARMUP_MODEL\",\"prompt\":\"ok\",\"stream\":false,\"keep_alive\":\"1h\"}" >/dev/null \
    && echo "[start] ✓ $WARMUP_MODEL 已驻留 (keep_alive=1h)" || echo "[start] ⚠ 预热失败, 检查模型是否存在"
else
  echo "[start] ✓ $WARMUP_MODEL 已在驻留中"
fi

# 2. 网关 :7800
if ! curl -s --max-time 2 -o /dev/null http://127.0.0.1:7800/v1/models; then
  echo "[start] 启动网关 :7800 ..."
  nohup "$PY" -m jiesuan_gateway --mode pool --port 7800 > /tmp/jiesuan_gateway.log 2>&1 &
  echo "[start] 网关 PID $!"
else
  echo "[start] 网关已在运行"
fi

# 3. 本机节点 (注册到网关)
if pgrep -f "jiesuan_node --name mac-air" >/dev/null; then
  echo "[start] 节点 mac-air 已在运行"
else
  echo "[start] 启动节点 mac-air ..."
  nohup "$PY" -m jiesuan_node --name mac-air --gateway 127.0.0.1:7800 > /tmp/jiesuan_node.log 2>&1 &
  echo "[start] 节点 PID $!"
fi

# 4. 等节点注册, 验证
sleep 3
MODELS=$(curl -s --max-time 5 http://127.0.0.1:7800/v1/models | grep -o '"qwen3.5:9b"' || true)
if [ -n "$MODELS" ]; then
  echo "[start] ✅ 就绪: http://127.0.0.1:7800/v1  |  控制台: http://127.0.0.1:7800/dashboard"
else
  echo "[start] ⚠️ 网关已起但模型列表为空, 节点可能还在注册, 稍后看 /tmp/jiesuan_node.log"
fi
