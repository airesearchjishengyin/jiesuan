# ADR-001 · 为什么节点引擎选 Ollama 而不是 vLLM

状态: 已采纳 · 日期: 2026-09-10 · 决策人: 借算项目

## 背景

借算的节点层需要选一个推理引擎作为"被调度的对象"。候选: Ollama vs vLLM（还有 llama.cpp server / SGLang / LMDeploy 等）。

## 决策

**v0.x 全线 Ollama 作为默认引擎；vLLM 作为高吞吐可选引擎（协议层已预留）。**

## 理由

| 维度 | Ollama | vLLM |
|---|---|---|
| 跨平台 | **macOS(Metal)+Windows(CUDA)+Linux 一键安装器** | Linux+CUDA 为主，macOS 只有实验性 CPU 后端 |
| 安装摩擦 | 下载即用，自带模型管理 | pip 环境+驱动+版本矩阵，Windows 更痛 |
| 模型管理 | `ollama pull x` 内建（GGUF 生态） | 手动下权重+配 tokenizer |
| 异构一致性 | **同一条命令在 M5/MX450/RTX 5060 上行为一致** | 不同硬件要调不同参数矩阵 |
| 量化生态 | GGUF Q4/Q5 即拉即用 | 主流是 FP8/AWQ，模型获取成本高 |
| 吞吐/并发 | 中（够 agent 单用户/小并发） | **高**（PagedAttention 连续批处理） |
| 生产场景定位 | 边缘/桌面/内网池 ← **借算的主场** | 数据中心/高并发服务 |

**核心判断**: 借算的第一性场景是" heterogeneous 边缘设备池"——节点是 MacBook Air / 游戏本 / Mac mini / N 卡台式机的**混合体**。vLLM 的吞吐优势只在"多并发+数据中心 GPU"时兑现，而这个场景下 Ollama 的跨平台零摩擦是压倒性的。工程上：一个 heterogeneous 池里跑两种引擎，运维成本×2，v0.x 不值得。

## 后果与演进路径

1. 协议层已预留: 节点只要暴露 **OpenAI 兼容端点**就是一等公民（`NodeEntry.kind`）。vLLM serve 起来就是 OpenAI 端点 → **vLLM 节点 = `--static-node name=x,url=http://ip:8000/v1` 一行接入**，零代码改动。
2. 触发条件: 当出现"单节点需要服务 >10 并发"的场景（真正的企业生产），该节点升格 vLLM；借算网关层不用改。
3. 性能天花板意识: Ollama 单节点吞吐有限（MX450 实测 7-9 t/s，M5 约 40-50 t/s），**池化本身就是对单节点吞吐不足的架构回答**——横向扩展比纵向换引擎便宜。

# ADR-002 · 异构节点接入哲学: "零 agent 接入"（声明式加入）

状态: 已采纳 · 日期: 2026-09-10

## 背景

用户核心需求: **新节点加入不能是"每台机器装一个 agent 并配环境"**——5060/新 Mac/任何 OpenAI 兼容引擎，必须是"执行硬性命令即可加入"。

## 决策

节点分两类，接入协议统一为**声明式**:

1. **engine 节点（零安装）**: 机器上只需要有一个 OpenAI 兼容服务（Ollama/vLLM/llama.cpp server/任何）。加入 = 网关侧一条命令:
   ```bash
   jiesuan-gateway ... --static-node "name=5060-box,url=http://192.168.0.50:11434,models=qwen3:14b"
   ```
   机器上**什么都不装**。网关直接把它的 OpenAI 端点纳入调度。
2. **agent 节点（可选增强）**: 想要心跳健康度/pending 信号/远程管理的机器，自愿装 `jiesuan-node`（一个 pip 包）。agent 只增收益不减功能——没 agent 的机器一样被调度。

## 为什么这才是对的（从 GPUsMarket/PAIR 学到的）

- GPUsMarket 要装 host CLI + 桌面 app → 每台机器都是一次"小型工程"；
- PAIR 要求每台机器装 router + Ollama + 登录配对；
- **LLM serving 的标准接口（OpenAI API）本身已经是"agent"**——再叠一层自研 agent 是重复建设。借算的 node agent 只做三件引擎不做的事: 心跳、计量、远程管理。engine 直连模式让"只有引擎的裸机"也能即刻入池。

## 后果

- `--static-node` 已实现（v0.1, 验证于 win-rtx）; 未来 5060 机器到位后:
  ```bash
  # 5060 机器上(一次性): 安装 ollama + 拉模型
  # 网关上(即时): 重启网关加一条 --static-node, 或用热加接口(见 v0.2)
  ```
- v0.2 计划: `POST /node/add` 热加接口 + `jiesuan node discover` 子命令（自动探测局域网内 11434/8000 端口）→ 把"声明式"进化成"自动发现"。
