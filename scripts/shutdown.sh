#!/bin/bash
# shutdown.sh — 彻底关闭借算: 停 jiesuan 进程 + 卸载 Ollama 全部驻留模型
# 用法:
#   scripts/shutdown.sh               # 停借算 + 卸载模型, 保留 Ollama 常驻 (其他任务可能共用)
#   scripts/shutdown.sh --kill-ollama # 连 Ollama 一起退出 (最彻底)
set -e
cd "$(dirname "$0")/.."

echo "[shutdown] 1/3 停止借算进程..."
pkill -f "jiesuan_node --name" 2>/dev/null && echo "  ✓ 节点已停止" || echo "  · 无节点进程"
pkill -f "jiesuan_gateway --mode" 2>/dev/null && echo "  ✓ 网关已停止" || echo "  · 无网关进程"

echo "[shutdown] 2/3 卸载 Ollama 驻留模型 (keep_alive=0)..."
.venv/bin/python - <<'EOF'
import json, time, urllib.request

def ps():
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/ps", timeout=5) as r:
            return [m["name"] for m in json.load(r).get("models", [])]
    except Exception:
        return []

models = ps()
if not models:
    print("  · Ollama 无驻留模型")
else:
    for m in models:
        body = json.dumps({"model": m, "keep_alive": 0}).encode()
        req = urllib.request.Request("http://127.0.0.1:11434/api/generate",
                                     data=body, headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=30)
            print(f"  ✓ 已卸载 {m}")
        except Exception as e:
            print(f"  ✗ 卸载 {m} 失败: {e}")
    time.sleep(1)
    left = ps()
    print(f"  剩余驻留: {left if left else '无 (全部释放)'}")

if "--kill-ollama" in __import__("sys").argv:
    pass
EOF

if [ "$1" = "--kill-ollama" ]; then
  echo "[shutdown] 3/3 退出 Ollama 应用..."
  osascript -e 'quit app "Ollama"' 2>/dev/null && echo "  ✓ Ollama 已退出" || echo "  · Ollama 未在运行"
else
  echo "[shutdown] 3/3 保留 Ollama (如需连它一起关: scripts/shutdown.sh --kill-ollama)"
fi

echo "[shutdown] 完成。内存压力:"
memory_pressure -Q 2>/dev/null | tail -1 || true
