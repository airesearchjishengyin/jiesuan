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

## 架构

```
agent/应用 ──OpenAI API──▶ Gateway(调度L0/L1/L2) ──▶ Node(Ollama) ×N
```

- **L0** RoundRobin · **L1** 负载感知 · **L2** temporal 预测器 (插槽, 学术线)
- 部署模式: `solo` 单机 / `pool` 内网池 / `network` 跨网 (v1.0)
- 红线: 永不内建支付/撮合; 默认内网; prompt 不落盘

## License

Apache-2.0 (待 v0.5 公开时生效)
