# 借算 (Jiesuan)

> 面向 agent 推理的边缘设备算力共享框架。把局域网内的闲置 Mac/PC 组成一个内存常驻的推理池: 一个 OpenAI 兼容 API 入口, 多台机器按策略分摊请求。

**状态**: v0.1 开发中 (私有仓库, 成熟后开源)

## 设计文档

- [DESIGN.md](docs/DESIGN.md) — v0.1 完整设计方案 (双轨分层/三模式/红线)
- [ch09-expert-prediction-survey.md](docs/ch09-expert-prediction-survey.md) — 专家时序预测学术综述 (L2 调度器的理论基础)
- [nvidia-expert-streaming-modules-2026-09-05.md](docs/nvidia-expert-streaming-modules-2026-09-05.md) — NVIDIA 生态调研

## 快速开始 (开发中)

```bash
# 节点端 (每台提供算力的机器)
pip install -e ./node
python -m jiesuan_node --name mac-air --gateway 192.168.x.x:7800

# 网关 (路由入口, 任意一台机器)
pip install -e ./gateway
python -m jiesuan_gateway --mode pool --port 7800

# 测试 (OpenAI 兼容)
curl http://localhost:7800/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3:14b","messages":[{"role":"user","content":"hi"}]}'
```

## 日常使用 (本机 Mac)

三个脚本在 `scripts/`, 不用记参数, 也不需要每次找 agent 开关:

```bash
~/jiesuan/scripts/start.sh    # 开机: 自动起 Ollama(若没跑) → 网关 :7800 → 本机节点 mac-air
~/jiesuan/scripts/stop.sh     # 关机: 停网关+节点 (不动 Ollama, 其他任务可能共用)
~/jiesuan/scripts/status.sh   # 看状态: 三个组件各自的存活情况 + 已注册模型列表
```

- **控制台**: http://127.0.0.1:7800/dashboard (打不开 = 网关没跑, 先 `status.sh` 再 `start.sh`)
- **Hermes 里用**: `/model js` 切到 jiesuan-local (127.0.0.1:7800/v1, 默认 qwen3.5:9b); 换模型后**首请求冷加载 1-2 分钟**, 之后约 5 秒
- **日志**: `/tmp/jiesuan_gateway.log` 和 `/tmp/jiesuan_node.log`
- **注意**: 脚本用 nohup 启动, 关掉终端不影响运行; 但 **Mac 重启后需要重新 `start.sh`** (如需开机自启可配 launchd, 找 agent)

## v0.3: mock 模式与 pull 模式 (逻辑外推测试 + 穿 NAT 入池)

### mock 模式 — 2 台真机模拟 N 台异构节点

```bash
# 模拟一台 8GB 只装 4b 的小机器, 30% 请求失败 (故障注入)
python -m jiesuan_node --name fake-1 --gateway 127.0.0.1:7800 --mock \
  --mock-spec "models=qwen3:4b,ram_gb=8,delay=0.3,fail_p=0.3"

# 模拟一台 32GB 大机器 (可注册大模型 → 验证异构路由)
python -m jiesuan_node --name fake-2 --gateway 127.0.0.1:7800 --mock \
  --mock-spec "models=qwen3:4b;qwen3:14b,ram_gb=32,delay=0.1"
```

- 输出与真实 Ollama ndjson 流完全一致, 网关与客户端零改动
- `--mock-spec` 参数: `models`(分号分隔) `ram_gb` `delay` `jitter` `fail_p` `busy=1`
- 可测: 异构路由 / 部分故障 / 负载竞争 / 容量指令回路 (load/unload 经心跳下发)
- 10–50 台逻辑外推 = 单机起 N 个 mock 进程; 真 GPU 性能数据仍需真机

### pull 模式 — 节点出站长连接, 穿 NAT (办公网/云主机/租用机器)

```bash
# 网关不变; 节点加 --pull 即可, 无需端口转发
python -m jiesuan_node --name office-mac --gateway <网关IP>:7800 --pull
```

- 原理: 节点主动向网关建出站 WebSocket (`/node/pull`), 请求沿连接下发 — 出站连接天然穿 NAT
- 与 push 模式 (默认) 可混跑: 网关按节点的 transport 自动选择下发路径
- pull 节点断线秒级暴露 (对比 push 需等心跳超时); 心跳与容量指令复用同一条 WS
- v0.3 限制: 单节点串行执行; 完整 ndjson 攒齐后一次回传 (网关侧收齐再返回)
- 适用: EigenFlux 志愿者办公机器 / GPUsMarket Docker 租用机 — 都在 NAT 后, 只有 pull 能入池

### push vs pull 怎么选

| | push (默认) | pull (--pull) |
|---|---|---|
| 网络要求 | 节点必须被网关主动连到 (同内网/可路由) | 只要节点能出站上网 |
| 流式 | 逐 chunk 透传 | 整体回传 (v0.3) |
| 断线感知 | 心跳超时 (~20s) | 连接断开即感知 |
| 适用 | 自家内网机器 | 办公网/云/租用机器 |

## 架构

```
agent/应用 ──OpenAI API──▶ Gateway(调度L0/L1/L2) ──▶ Node(Ollama) ×N
```

- **L0** RoundRobin · **L1** 负载感知 · **L2** temporal 预测器 (插槽, 学术线)
- 部署模式: `solo` 单机 / `pool` 内网池 / `network` 跨网 (v1.0)
- 红线: 永不内建支付/撮合; 默认内网; prompt 不落盘

## License

Apache-2.0 (待 v0.5 公开时生效)
