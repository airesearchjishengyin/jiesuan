# 公网部署指南 (v0.3+) — 跨网机器入池

## 架构

```
租用/志愿者节点 (GPUsMarket/办公网, NAT 后)
    │ 出站 wss (唯一要求: 能出站上网)
    ▼
Cloudflare 边缘 ──tunnel──▶ 网关 (你的 Mac / 未来 VPS :7800)
    ▲
    │ OpenAI API + Bearer key
客户端 (Hermes / 评测脚本)
```

关键设计: **所有外部节点一律 pull 模式**。节点主动向网关建出站 WebSocket,
无需公网 IP、无需端口转发, 穿任意 NAT/防火墙。CF 免费 100s 空闲超时由
5s 心跳天然覆盖, 无需额外 keepalive。

## 一次性配置 (网关侧)

### 1. 生成密钥

```bash
cd ~/jiesuan
printf 'JIESUAN_NODE_TOKEN=%s\nJIESUAN_API_KEY=%s\n' \
  "$(openssl rand -hex 16)" "$(openssl rand -hex 16)" > .env
chmod 600 .env        # 节点 token 千万不要进 git (已 gitignore)
```

- `JIESUAN_NODE_TOKEN`: 节点注册凭证 → 分发给每台要入池的机器
- `JIESUAN_API_KEY`: 客户端调用凭证 → 自己用, 别外泄

### 2. 隧道 (推荐独立隧道, 不与现有服务混用)

```bash
cloudflared tunnel create jiesuan
cat >> ~/.cloudflared/config.yml <<EOF
  - hostname: js.macagents.org        # 或你自己的域名
    service: http://localhost:7800
EOF
cloudflared tunnel route dns jiesuan js.macagents.org
cloudflared tunnel run jiesuan       # 或配成 launchd 常驻
```

### 3. 重启网关 (start.sh 会自动加载 .env 启用鉴权)

```bash
scripts/stop.sh && scripts/start.sh
```

## 节点接入 (在每台远程机器上)

### GPUsMarket Docker 租用机

```bash
# 容器内 (有 root): 装 python3.11+ 后
export JIESUAN_NODE_TOKEN=<你的节点token>
pip install aiohttp yarl
git clone <repo> && cd jiesuan   # 或只拷 node/ 目录
python -m jiesuan_node --name gpm-<编号> \
  --gateway wss://js.macagents.org --pull \
  --engine http://127.0.0.1:11434   # 机器上需有 Ollama + 模型
```

### 验证入池

```bash
# 本地: 网关节点表应出现 gpm-<编号> (transport=ws)
curl -s http://127.0.0.1:7800/nodes | python3 -m json.tool

# 远程: 用 API key 打一发
curl https://js.macagents.org/v1/chat/completions \
  -H "Authorization: Bearer <API_KEY>" -H 'Content-Type: application/json' \
  -d '{"model":"<模型名>","messages":[{"role":"user","content":"hi"}]}'
```

## 安全模型 (公网红线)

| 威胁 | 防线 |
|---|---|
| 陌生节点注册偷请求/投毒 | `--node-token` 校验, 错 token 连接即断 (exit 3 不重连) |
| 网关地址泄露被白嫖 | `/v1` 强制 Bearer key |
| 明文监听 | 全链路 wss (CF 边缘 TLS) |
| **prompt 流经陌生宿主** | **架构红线: 真实用户流量禁止路由到租用节点** — 租用节点只跑合成负载/基准测试; L2 学术数据不含隐私 prompt (prompt 不落盘的公网延伸) |

## 已知限制 / Roadmap

- pull 节点单连接串行执行 (ws_lock); 高并发需多连接隧道 (roadmap: 多路复用)
- pull 回传是攒齐整体 ndjson, 网关侧不流式透传 → 客户端拿到的响应非 chunked
- CF 免费版 100s 空闲超时: 心跳 5s 已覆盖; 若改长心跳需加应用层 ping
- 大规模 (>500 节点) 时网关需迁 VPS 并考虑多进程分片 (aiohttp 单进程实测 50 节点内存 ~2GB 级)
