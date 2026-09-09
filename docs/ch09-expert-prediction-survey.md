# Ch08 · MoE 专家时序/激活预测：学术工作综述 (2026-09-05)

定位: 服务 M1 模拟器（预测策略对比 harness）与后续论文。前作: Ch07 (slotstream router-trace, entropy 0.761, top-1 persistence 63.8% = 20× random, set-Jaccard 0.226, prefetch ceiling +13%)。
本文回答: "专家预测"这个方向别人做到哪了、用什么信号、我的 temporal 视角站在哪、空位在哪。

---

## 0. TL;DR

1. 专家预测已经是活跃子领域，按"要不要改模型/要不要训练"分三条路线：**零训练跨层外推**（Fate、ProMoE）、**改结构重训**（Pre-gated MoE、melinoe）、**轻量探针**（ETH Pre-Attention、MoE-Infinity traces、SPICE draft）。
2. 最强公开精度来自**跨层 gate-input 相似度**这条线（Fate: 命中率 99%，decode 4.1×），核心物理依据是相邻层 gate 输入的高余弦相似度——**零训练、零额外 GPU 开销**。
3. 我的 Ch07 temporal 视角（层内时间持续性 persistence / Jaccard 集合衰减 / 熵）与上述全部工作**正交**：他们预测"下一层的专家"（空间=层间），我测的是"同一位置的专家随时间变不变"（时间=token 轴）。两条信号可叠加，且时间轴信号直接服务**放置/admission**（他们全部只做 prefetch）。
4. 四个明确空位：① 没有公开的 wild-trace 预测器基准（各家自测自夸）；② persistence→缓存 admission/放置策略无人做；③ **EP 集群的专家放置预测**（网络版）完全空白；④ 异构消费级机队下的预测-放置联合优化空白。

---

## 1. 三条技术路线

### 1.1 零训练 · 跨层外推（当前精度/性价比之王）

| 工作 | 机制 | 关键数字 | 备注 |
|---|---|---|---|
| **Fate** (arXiv 2502.12224) | 用第 L 层 gate inputs 直接预测 L+1 层专家（相邻层 gate 输入余弦相似度高）；shallow-favoring 浅层偏好缓存；定制量化 | 预测命中率 **99%**；prefill 4.5×/1.9×，decode **4.1×/2.2×**（vs Load-on-Demand / 激活路径法）；跨内存预算可扩展 | 边缘 offload 场景；零训练零开销；**复现价值最高** |
| **ProMoE** (Song et al.) | 层间残差信息预测高负载专家 + proactive caching | （数字以原文为准，待精读） | prefetch 精度导向；常被后续工作引用为 baseline |

物理本质: 路由器输入（gate 看到的 hidden）相邻层变化平缓 → 上一层的选择 ≈ 下一层的选择。这解释了为什么"预测"可以便宜到免费。

### 1.2 改结构 · 重训（提前一整层拿预测窗口）

| 工作 | 机制 | 代价/收益 |
|---|---|---|
| **Pre-gated MoE** (Hwang et al., ISCA'24, 开源) | 给每层加一个预测 gate，**提前一整层**输出下一层路由 → 预取窗口 = 整层计算时间，offload 预取完全免费 | 结构改动 = 必须重训 = 模型质量有代价（后续工作普遍引用这点）；端到端收益 offload 场景个位数% ~ 1.7×（以论文为准，引用前核对） |
| **melinoe** (arXiv 2602.11192) | 训练时加辅助 loss 让路由**集中化** → 部署时专家缓存命中率大增 | 1.2-3×，最高 14.7×；改训练管线，对现成 checkpoint 不可用 |

定位: 这条线证明"可预测性"本身是**可训练的属性**——但对你（拿现成 checkpoint 做推理系统研究）只有引用价值，没有可用性。

### 1.3 轻量探针 · 激活回归（不动结构、可插拔）

| 工作 | 机制 | 关键数字 |
|---|---|---|
| **Pre-Attention Expert Prediction** (Zhu et al., ETH, arXiv 2511.10676) | 指出"用上一层激活预测"精度低且首层无解；用 attention 前的信息 + ranking-preserving 线性函数预测专家排序 | 轻量、可插拔；精度高于跨层外推（数字待精读核对） |
| **MoE-Infinity** (OSDI'24) | **激活统计 trace**（序列级激活模式）驱动 expert cache 替换与预取 | 每 token 延迟改善 **2.7-13.7×**（vs vLLM/Ollama/DeepSpeed）；个人机器场景的开山系统 |
| **SPICE** (arXiv 2608.21240) | 轻量 draft 模型预测 + **置信度门控**：高置信拉取、低置信 miss 用 shared expert + **LoRE 低秩替代**、精确残差异步 CPU 算 | TPOT 最高 **3.12×**，质量损失极小；"miss 分级处理"是 2026-08 最新 SOTA |
| **Fast MoE via Predictive Prefetching + Expert Replication** (2605.11537) | 简化 gate 快速训练 + 热专家复制到多机 | 面向集群并行 |

### 1.4 系统层使用预测信号的方式（预测只是输入，怎么用更重要）

| 系统 | 用法 | 收益 |
|---|---|---|
| **MoEpic** (2509.08342) | 预测+预取 + **专家纵切**（热专家上半段常驻，同预算存 2× 专家）+ 定点迭代自适应配置 | 延迟 −37.5~65.7%，省一半 GPU |
| **HOBBIT** | 混合精度专家（热全精度冷 1-bit）+ 预测驱动加载 | decode 9.93× vs SOTA offload（Jetson） |
| **SP-MoE** (2510.10302) | 投机解码 + 预取联合 | Mixtral 场景 |
| **2608.12103** (kernel-managed tiering) | 诚实反例: GH200 上 OS LRU ≈ oracle 频率表（1.09×），但 **oracle 表在 domain shift 下崩溃**（74%→21-34%），LRU 稳定 70%+ | **教训: 不要做静态热表，做运行时 recency** |
| **2607.05116 / 2508.12851** | 通信感知的专家**放置**+剪枝 / 激活感知放置+**运行时迁移** | 数据中心 fabric 假设 |

---

## 2. 预测信号的"物理来源"清单（M1 策略设计的素材库）

1. **gate-input 余弦相似度**（层间）— Fate 的免费午餐;
2. **残差流相似度**（层间）— ProMoE;
3. **attention/前路由信息**（同层早信号）— ETH 2511.10676;
4. **训练期嵌入的结构性集中**（跨层一致性是被训练出来的）— Pre-gated / melinoe;
5. **序列级激活 trace**（token 间统计）— MoE-Infinity;
6. **阶段级一致性**（CoT 推理的阶段切换处激活模式连贯）— SAEM (1.33-1.54×);
7. **置信度**（预测的置信分布本身是信号）— SPICE;
8. **层内时间持续性**（persistence / Jaccard / 熵）— **Ch07（本文作者自己的数据，公开文献中无对应系统实现）**。

注意 1-7 全部服务于 **prefetch**（下一步拉谁），没有一条服务于 **placement/admission**（长期谁驻留谁驱逐）——除了 8 的时间统计性质天然适合。

## 3. 与 Ch07 的对照与差异化

| 维度 | 现有工作 (1.1-1.3) | Ch07 |
|---|---|---|
| 预测轴 | 空间: 第 L 层 → 第 L+1 层 | 时间: token t → t+Δ（同位置） |
| 信号 | gate/residual 相似度（局部、免费） | persistence 63.8% = 20× random、set-Jaccard 0.226、熵 0.761 |
| 用途 | prefetch | 放置 / admission / EP 共置 |
| 验证 | 论文各自自测 | 需 wild 分布重测（**最大风险: lab prompt 过拟合**） |
| 可叠加性 | ✓ 正交信号，联合使用是增量点 | 同左 |

## 4. 空位清单（M1 的靶子，按发表可行性排序）

1. **公开 wild-trace 预测器基准**: 统一 trace 格式（兼容 slotstream router-trace JSON）+ 统一指标（命中率/延迟投影/内存）+ 各路线复现。各家数字不可比是公开痛点。→ 最低垂的果实。
2. **persistence→admission/placement**: 把时间持续性变成缓存策略（谁值得常驻）与 EP 共置策略（哪俩专家该同机）。现有文献 0 覆盖。
3. **预测器在异构消费级机队上的联合放置-调度**: 结合 PAIR 的请求路由缺口（cold-load 无感知、无容量模型），专家粒度放置预测 = 无人区。
4. **置信度门控的国产化复现**: SPICE 匿名仓库未开源，复现+改进（LoRE 换成 Ch07 熵驱动）可成文。

## 5. 引用清单

- Fate: arXiv 2502.12224 (Accurate Expert Predictions via Cross-Layer Gate)
- Pre-gated MoE: ISCA'24, github.com/ranggihwang/Pregated_MoE
- Pre-Attention Expert Prediction: arXiv 2511.10676 (ETH Zurich)
- ProMoE: Song et al. (proactive caching, 数字待核对)
- MoE-Infinity: OSDI'24 (openreview BL7WMLJKZM)
- MoEpic: arXiv 2509.08342
- SPICE: arXiv 2608.21240
- melinoe: arXiv 2602.11192
- SP-MoE: arXiv 2510.10302
- Predictive Prefetching + Replication: arXiv 2605.11537
- kernel-managed tiering: arXiv 2608.12103
- Communication-aware placement: arXiv 2607.05116; activation-aware placement+migration: arXiv 2508.12851
- HOBBIT / SAEM (2608.21614) / FluxMoE (2604.02715): 见 nvidia-expert-streaming-modules-2026-09-05.md

---

## 附录 A · 个人机器 serving 平台（赚钱路线，2026-09-05 快查）

| 平台 | 收什么机器 | 模式 | 备注 |
|---|---|---|---|
| **Dark Bloom** | **Mac**（专攻 Apple Silicon） | 分布式推理网络，Mac 主人共享闲置算力换钱，去中心化路由 | 自称 3.2B+ tokens 生成、~9,400 t/s 持续带宽；**与你现有 Darkbloom 工具链/income path 直接对口**（darkbloom-provider skill） |
| **GPUsMarket** | RTX 4090/3090、RX 7900 | Ollama 挂机出租，$3-10/天 | 需 NVIDIA，Mac 无关 |
| **Kuzco → Inference** (Solana) | GPU（含消费级） | OpenAI 兼容 API 网络，积分/代币 | 纯 GPU |
| **io.net / Nosana / Salad / Vast.ai** | NVIDIA 显卡为主 | DePIN 出租市场 | 大多要求 CUDA，Apple Silicon 支持弱或无 |
| **elvron / Infernet / AIIGo** | 手机/笔记本/PC | 去中心化算力市场 | 早期，注意审计风险 |

判断: **"接入现成平台"路线成立且你已有入口**（Dark Bloom 生态 + teamorouter 合作）。现实预期: 单台 Mac 收益是零花钱级（对齐 GPUsMarket 的 $3-10/天量级），真正的杠杆是 ① 规模（你的办公室机队叙事）② 学术研究提高机队利用率（调度策略 → 同样硬件多赚钱）→ 这正是学术与赚钱两条线的汇合点: 你的 M1/预测器研究直接提升你在这些平台上的单位算力收益。
