# 借算 · 部署与运维手册 (DEPLOYMENT)

> 本文档面向"把借算部署到生产环境"的运维者。设计文档见 [DESIGN.md](DESIGN.md)，架构决策见 [ADR-001](ADR-001-engine-and-nodes.md)。

---

## 1. 拓扑与角色

```
                ┌────────────────────────┐
agent/应用 ──▶ │ Gateway :7800 (任意一台机器) │
                └───────┬────────────────┘
        OpenAI 兼容路由 + 调度 + 计量
                        │ HTTP (OpenAI 兼容)
        ┌───────────────┼────────────────┐
        ▼               ▼                ▼
   [mac-air]      [win-rtx]        [5060-box ...]
   Ollama:11434   Ollama:11434     Ollama/vLLM:8000
   (agent 节点)    (engine 节点)     (engine 节点)
```

- **Gateway**: 唯一入口，OpenAI 兼容 (`/v1/chat/completions`)。可部署在池内任意机器，也可单独一台。
- **Node 两类**:
  - `engine 节点`: 机器上只有推理引擎（Ollama/vLLM），**零借算安装**。网关声明式接入。
  - `agent 节点`: 额外装 `jiesuan-node`（pip 包），提供心跳/pending 信号/远程管理。

## 2. 部署 Gateway

```bash
# 依赖: Python 3.11+, pip
git clone <repo> && cd jiesuan
python -m venv .venv && ./.venv/bin/pip install -e ./gateway -e ./node litellm

# 启动 (生产推荐: scheduler=litellm)
./.venv/bin/jiesuan-gateway --mode pool --port 7800 \
  --scheduler litellm \
  --static-node "name=win-rtx,url=http://192.168.0.104:11434,models=qwen3:4b" \
  --static-node "name=mac-air,url=http://127.0.0.1:11434,models=qwen3:14b;deepseek-r1:14b;gemma4:e4b"
```

### 调度器选择

| `--scheduler` | 行为 | 何时用 |
|---|---|---|
| `L0` | 轮询 | 基线对照 |
| `L1` | pending 最少优先 | 无 LiteLLM 依赖的轻量部署 |
| `litellm` (L-Prod) | 延迟感知路由 + 失败冷却(20s) + 2 次重试 + failover | **生产默认** |
| `L2` (规划) | temporal 预测器 | 实验分支 |

### Gateway 常驻 (生产必做)

**macOS (launchd)**:
```bash
cat > ~/Library/LaunchAgents/com.jiesuan.gateway.plist <<'EOF'
(内容用 launchd plist 模板: ProgramArguments 填上面启动命令, KeepAlive=true)
EOF
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.jiesuan.gateway.plist
```

**Windows**: `schtasks /create /tn JiesuanGateway /tr "..." /sc onstart /rl highest /f`
**Linux**: systemd unit 同理（`Restart=always`）。

## 3. 节点接入

### 3.1 engine 节点（零 agent，推荐起步）

机器侧唯一要求: 跑一个 OpenAI 兼容引擎。

**Ollama (Mac/Win/Linux 通用)**:
```bash
# 安装: https://ollama.com/download  (Windows 下载 OllamaSetup.exe)
ollama pull qwen3:14b          # 拉需要的模型

# Windows 特有: 允许局域网访问
setx OLLAMA_HOST 0.0.0.0       # 然后重启 ollama serve
# 防火墙放行 11434
netsh advfirewall firewall add rule name="Ollama" dir=in action=allow protocol=TCP localport=11434
```

**vLLM (Linux + NVIDIA, 高并发场景)**:
```bash
vllm serve Qwen/Qwen3-14B --port 8000
```

**网关侧接入（一条命令，机器上零安装）**:
```bash
jiesuan-gateway ... --static-node "name=5060-box,url=http://192.168.0.50:11434,models=qwen3:14b;qwen3:8b"
# vLLM 节点:
jiesuan-gateway ... --static-node "name=vllm-1,url=http://192.168.0.60:8000/v1,models=qwen3-14b"
```

### 3.2 agent 节点（可选增强）

```bash
# 节点机器上
pip install -e ./node
python -m jiesuan_node --name mac-air --gateway <gateway-ip>:7800
# Windows 服务化:
schtasks /create /tn JiesuanNode /tr "python -m jiesuan_node --name win-rtx --gateway 192.168.0.103:7800" /sc onstart /rl highest /f
```

### 3.3 节点健康与验收清单

```bash
curl http://<gateway>:7800/nodes          # 所有节点 alive=true
curl http://<node>:11434/api/version      # 引擎可达
# 端到端:
curl http://<gateway>:7800/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"qwen3:14b","messages":[{"role":"user","content":"ping"}]}'
```

## 4. 运维手册 (生产环境)

### 4.1 已知故障模式与自愈

| 故障 | 现象 | 自愈机制 | 人工干预 |
|---|---|---|---|
| 节点睡眠/断网 | `alive=false`，调度剔除 | litellm cooldown 自动摘流 | 恢复网络/唤醒后心跳自动回归 |
| Ollama 进程僵死 (Windows) | 引擎端口通但请求超时 | litellm 重试→冷却摘流 | **SSH 重启**: `taskkill /F /IM ollama.exe && schtasks /run /tn OllamaServe` |
| 模型未加载 (冷启动慢) | 首请求 20s+ | litellm retry 覆盖 | 预热: 部署后发一次 dummy 请求 |
| Gateway 崩溃 | 全池不可达 | launchd/systemd KeepAlive 自动拉起 | 无 |
| 显存 OOM (Windows 小显存卡) | 引擎 500 | litellm failover 到其他节点 | 换小模型或加节点 |

### 4.2 监控要点

```bash
# 实时节点表
watch -n 30 'curl -s http://localhost:7800/nodes | python3 -m json.tool'

# trace 分析 (请求路由/延迟分布/错误率)
tail -f trace.jsonl | python3 -c "
import json,sys
from collections import Counter
c=Counter()
for l in sys.stdin:
    d=json.loads(l)
    if d['event']=='route': c[d['node']]+=1
    print(d)
"
```

### 4.3 安全基线（内网部署）

1. Gateway 只绑内网接口；**不要**暴露到公网（无认证层，v0.2 加入 API key）；
2. Windows 节点: SSH 用密钥禁密码（`sshd_config: PasswordAuthentication no`）；
3. prompt 不落盘是默认行为；企业审计需求才开 trace 的 prompt 记录（当前 trace 只记元数据）。

## 5. 新模型接入 SOP

场景: 上游发布了新模型（如 qwen3.5:14b），要全池升级。

```bash
# 1. 每个节点拉模型 (可并行)
ssh mac-air  "ollama pull qwen3.5:14b" &
ssh win-rtx  "ollama pull qwen3.5:14b" &
wait
# 2. 网关刷新 (engine 节点: agent 心跳自动带新模型表; static 节点: 更新 --static-node 参数重启)
# 3. 验收: 每节点发一次 dummy 请求确认加载
```

**零代码改动** — 新模型/新引擎/新节点都是配置操作，这是借算的核心设计承诺。

## 6. v0.2 运维 Agent 路线图（自动运维）

设计目标: 上述"人工干预"列全部自动化。

```
jiesuan-ops (守护进程, 跑在 gateway 机器上)
  ├─ 健康巡检: 每 60s 对每节点发 dummy 请求 → 比对 P95 延迟基线
  ├─ 僵死检测: 引擎端口通但请求超时 → 自动 SSH 重启引擎 (Windows: schtasks / Mac: launchctl)
  ├─ 自动拉起: 节点失联 > 5min → WOL 魔术包 (需交换机支持) + 通知
  ├─ 模型预热: 按请求热度 Top-K 预加载到对应节点 (temporal 预测器的第一个应用)
  └─ 通知通道: webhook → Telegram/企业微信
```

验收标准（对应 Ch.08 先导实验指标）: 72h 无人值守，节点故障 5 分钟内自愈率 ≥ 90%，P99 延迟 < 2s（内网）。

## 7. 故障排查速查

```bash
# 网关起不来: address already in use
lsof -iTCP:7800 -sTCP:LISTEN   # 找到旧进程 kill

# 节点请求全部 502
curl http://<node>:11434/api/version   # 引擎死了 → 重启引擎
curl http://<gateway>:7800/nodes       # alive=false → 网络/睡眠问题

# Windows Ollama 不听局域网
setx OLLAMA_HOST 0.0.0.0 && 重启 OllamaServe 计划任务
netstat -ano | findstr 11434           # 应显示 0.0.0.0:11434 LISTENING

# qwen3 系列模型回复 content 为空
# → 思考模型: 内容在 message.thinking 字段; 前传 "think": false 或加大 num_predict
```
