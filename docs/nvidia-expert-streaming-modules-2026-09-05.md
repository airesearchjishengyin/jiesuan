# NVIDIA 卡上的 slotstream 式优化：方案盘点 + 可构建模块清单

日期: 2026-09-05 · 前置: slotstream-intel.md (Mac 侧情报)、lab-notes-2026-09-04-16gb-cliff.md (比值定律)、上一轮 NVIDIA 初步调研 (@session:default/20260905_090455_b4700a)
本轮增量: 不重复盘点，深挖代码/PR/RFC 现状，落位"能做什么模块"。

---

## 0. TL;DR

1. NVIDIA 低端卡上"专家流式化"已是活跃赛道，且正在**收敛成两个工程中心**: llama.cpp（C++/CUDA，RFC 未合并、多 fork 并存）和 vLLM（Python，RFC 三阶段 PR 图纸，PR1 已过 CI）。自助实现（tinyserve、Lidenburg、SharkWipf、msx98 fork）全部验证了方向。
2. **所有方案共用的三件套**: CPU(pinned/DRAM) 池 + GPU 定址 slot 池 + 预测/预取。分歧只在预测器（频率统计 / 激活回归 / draft 模型 / 时间局部性）和 miss 处理（CPU 算 / PCIe 拉取 / 低秩替代）。
3. **真实的空位**（2026-09 时点，检索多轮确认）:
   - a) **时间局部性预测器没有系统性的开源实现和公开对比**——你 Ch07 的方法论（top-1 persistence 63.8%、set-Jaccard 衰减、熵）正好是这个空位的钥匙;
   - b) **没有标准化的 expert-access trace 数据集 + 策略回放模拟器**——各家论文各测各的，无法横向比较;
   - c) **NVMe→RAM→VRAM 完整三层（slotstream ExpertStore 的忠实移植）在 CUDA 上没人做好**——现有都是两层（RAM→VRAM），NVMe 只在 Lidenburg 的 SATA SSD 实验里出现过;
   - d) llama.cpp RFC 的**小/老 GPU 回归问题**（1080 Ti 全档负收益）是公开的未解问题。
4. 硬件事实再次确认: 同样的模型在 N 卡上预期比 16GB Mac 快 10-30×（tinyserve: gpt-oss-20b 8GB 卡 30 t/s; llama.cpp RFC: 284B 模型 2×3090 17-22 t/s; 你的 Mac: Flash-Next 1.06 t/s）。

---

## 1. 方案盘点（本轮深挖后的事实，含代码状态）

### 1.1 llama.cpp 生态: RFC #24528 (leloch) — 最接近 slotstream 的设计

| 项 | 事实 |
|---|---|
| 设计 | MUL_MAT_ID **留在 CPU**；命中行由 CPU kernel 内 thread-0 派一个 batched matvec 到 GPU 算，miss 行照常 CPU 算。**无 PCIe 同步点、worst case 退化为 vanilla**。VRAM 池 + 专家→slot 记账 |
| 代码 | `leloch/llama.cpp` 分支 `moe-cache-pr`（~1700 行 CUDA，`moe-cache.cu/.cuh` + 后端无关函数表 + CPU 钩子 + `--moe-cache` flag）。**仍在活跃迭代**: v1(6/12) → v2 → v3 → 7/26 "rework MoE expert cache execution"。**未合并**（讨论 31 回复，无 maintainer 结论） |
| 实测（作者，4×3090 + 8ch DDR4） | GLM-5.1 754B IQ2_M **+25%** (17.49 t/s)；Qwen3.5 397B +7%；装得进 VRAM 的模型 parity（缓存休眠）；13 模型 16/16 ≥ parity，ppl 统计不变 |
| 独立复现 (noonghunna, 2×3090, 118B/284B, 日用数周) | 覆盖率→命中率→速度**线性到 31% 覆盖(~10k experts)无拐点**；TTFT 0.6s→0.21s（**延迟收益 > 吞吐收益**）；**4→8 内存通道(+75% 带宽)只换 +6% decode——缓存已吃掉带宽墙**，把 RAM 带宽问题转化成 PCIe 延迟问题 |
| 反例 (batot1, GTX 1080 Ti 11GB) | **全档回归**（4GB 缓存 −31%，32MB 缓存 −3%）；小老卡无 tensor core、dense 层吃掉 VRAM 后无余量，CPU 算专家反而更快。bail-out/trim 后也不回 baseline。`--moe-cache auto` CLI 解析 bug |
| 已知坑 | batch 超 cache max batch 时**静默拒绝所有 decode 无诊断**（noonghunna 踩过，误判 −30%）；大池需要等比例变暖，**欠变暖读数像饱和**（"top-1000 专家就够"是测量假象，SharkWipf 的拐点观察被 noonghunna 用更长流量推翻） |
| 其他 fork | Lidenburg（单文件 ggml-backend.cpp，+60%，无 PR 意愿）; SharkWipf（GLM 5.2 +30-50%，3 卡并 1 卡反而更快） |
| 关联线程 | #20757（3× decode 回归 + Metal slot-pool 97-99% 命中率仍 2× 慢——**同步点之罪**）、#23170 post-mortem（staging buffer 修对后是 no-op，**必须持久缓存+显式记账**）、#21067 prefetch PR、#22584 静态放置 |

### 1.2 vLLM 生态: RFC #38256 (tinyserve 作者) — 三阶段图纸

| 项 | 事实 |
|---|---|
| 架构 | `ExpertWeightProvider` ABC: `FullGPUProvider`(零开销直通) / `CachedWeightProvider`(GPU LRU + CPU pinned backing)。挂在 `FusedMoEModularMethod.apply()`，**kernel 不知道权重从哪来**；topk_ids 永远重映射成 slot index（torch.compile/CUDA graph 兼容：GPU buffer 定址、映射张量图外 CPU 更新） |
| PR1 [#37190] | ~600 行 Python，**open, CI passing**: 同步 H2D、LRU(OrderedDict)、BF16+FP8、batched prefill 去重（3K ctx top4 32专家: 12K 次顺序加载→32 次，375×↓）、`--enforce-eager` |
| PR2 (未动工) | **异步 H2D + cross-layer temporal prediction** ← 你的 Ch07 方法论可以在这里落地 |
| PR3 | 磁盘层(mmap)、更多量化、EPLB、遥测 |
| tinyserve 生产数据 | RTX PRO 2000 8GB + **gpt-oss-20b MXFP4**: 30 t/s 稳定到 32K ctx（StreamingLLM 平坦 29 t/s），FP8 KV → 53K ctx，**vs HF device_map=auto 155×**，temporal 预测命中率 97-100% |
| 设计哲学 | RFC 明确问: "LRU 该不该做成 ARC-ready 的 policy ABC?" ← **策略接口本身就是开放问题** |

### 1.3 学术前沿（2025H2-2026）

| 系统 | 机制 | 数字 | 对空位的启示 |
|---|---|---|---|
| **MoEpic** (2509.08342) | 专家**纵向切分**: 热专家上半段驻留 VRAM（同预算存 2× 专家数），下层专家预测预取，定点迭代自适应配置 | 延迟 −37.5~65.7%，省一半 GPU | "切分专家"是缓存维度的正交创新，没人跟 slotstream 的 record 布局结合过 |
| **SPICE** (2608.21240, 2026-08) | 投机预取 draft 模型 + 置信度门控: 高置信拉取、低置信 miss 用 resident shared expert + **LoRE 低秩替代**、精确残差异步 CPU 算 | TPOT 最高 3.12×，质量损失极小 | "miss 分级处理"——不是所有 miss 都值得拉全量专家 |
| **FluxMoE** (2604.02715) | CUDA VMM (`cuMemMap`) PagedTensor: 逻辑地址固定、物理页动态绑定，专家即用即逐，VRAM 让给 KV | 内存受限 3.0× | 用驱动层虚拟内存替代手写 slot 记账——与 slotstream 的定址 slot 是两条路线，**没人对比过** |
| 2511.10676 | "ranking-preserving 线性函数预测专家排序"（轻量激活探针） | — | 预测器可以比 draft 模型便宜得多 |
| 2608.11688 | 边缘 MoE offloading | — | 边缘场景 = 你的 16GB 目标画像的 N 卡版 |

（Fiddler/KTransformers/PowerInfer/MoE-Infinity/HOBBIT/Mixtral-offloading 见上轮报告，机制不变: 送激活过 PCIe 便宜于送权重、热冷分层、power-law 预载。）

---

## 2. 收敛结构: 大家都在做同一台机器

```
NVMe ──io_uring/pread──▶ DRAM pinned 池 ──PCIe async──▶ VRAM slot 池(定址) ──▶ 融合 kernel
                              ▲                        ▲
                        (可选第三层,             预测/预取器 ←—— 本轮全部分歧点
                         没人做好)                LRU/LFU │ 频率census(leloch) │ 激活回归(MoEpic/2511.10676)
                                                                │ draft模型+置信度(SPICE) │ 跨层temporal(vLLM PR2/tinyserve)
                                                                ▼
                                                        miss 处理: CPU算(leloch/Fiddler) │ 全量拉(vLLM/FluxMoE) │ 低秩替代(SPICE)
```

你的 slotstream Ch07 资产在图中的位置: **预测器一栏里"逐层时间局部性"（persistence/Jaccard/熵）没有任何系统实现**——tinyserve 的 "temporal prediction" 是 cross-layer 的，你的 Ch07 是层内时间序列视角（top-1 persistence 63.8% = 20× random、prefetch ceiling +13%）。

---

## 3. 可构建模块清单（按"别人已验证的地基 + 你能加的层"组织）

### M1 · 时间局部性预测器 + 公开对比 harness 【最贴合你的资产，纯 Python 起步】
- **是什么**: 把 Ch07 的逐层 temporal metrics 做成在线缓存策略（admission/prefetch 信号），与 LRU/LFU/频率census/激活回归/draft模型 在**同一 trace、同一模拟器**下对比。
- **别人缺什么**: 每家自带私货预测器，无公开横向对比; "时间局部性是否优于频率统计"在 N 卡栈上无答案（2608.12103 只证明 LRU≈oracle 频率且比静态表稳，但没测 temporal 信号）。
- **形式**: ① trace 采集器（llama.cpp `--router-trace` 类钩子或 vLLM hook，你 slotstream 里已有 router-trace 实现）; ② 回放模拟器（trace + 池大小 + 策略 → 命中率 + PCIe 延迟模型 → 投影 t/s）; ③ 报告/论文。
- **硬件需求**: 起步为零（CPU 跑模拟器）; 验证需租卡（autodl 4090 ~¥2/h 级别）。
- **工作量**: 模拟器+策略 ~1-2 周; 采集钩子 +1 周。
- **风险**: temporal 信号在真实对话分布上可能弱于你的 lab 提示——用 ShareGPT/wildchat 类分布重新验证是第一步（你 Ch07 用的是固定 lab prompt，这是最大的实验设计风险）。

### M2 · vLLM PR2 的 cross-layer temporal prediction 实现 【Python，高可见度】
- **是什么**: RFC #38256 PR2 明确计划做 cross-layer temporal prediction 且未动工; RFC 开放问题 #4 在征求 policy ABC 设计。用 Ch07 方法论实现 + 在 PR1 的接口上提交。
- **别人缺什么**: PR1 只有 LRU; PR2 空缺。
- **前提**: 盯 #37190 合并状态; 若 PR1 长期不合并，则在 tinyserve（MIT, 独立实现, 30 t/s 已验证）上做同样的事，它更小更好改。
- **工作量**: 预测器本体 ~300-500 行 Python; 难点在 torch.compile 边界外运行约束。
- **适配你的技能栈**: 最好——asyncio/Python 工程化是你的主场。

### M3 · llama.cpp RFC 生态的修复与最小核 【C++/CUDA，影响力最大】
- **是什么**: leloch 分支活跃但未合并，社区明确列出的可收割项:
  1. batch > cache max batch 静默失败 → 加诊断（noonghunna 说 patch 现成，可转 upstream）;
  2. `--moe-cache auto` CLI stoi bug;
  3. 1080 Ti 小卡回归 → RFC 自己提出的 "minimal core"（砍 fused GLU/handoff/hot-set，代码减半保主要收益）;
  4. bail-out/trim 后不回 baseline 的路径修复。
- **别人缺什么**: 提案者要 benchmark 机器，maintainer 要维护性证据; 缺一个系统性的 "什么硬件 regime 下该开/关" 的 autotuner——这恰好是个可写的小模块（VRAM 余量 × GPU SM 性能 × RAM 带宽的决策函数，1080 Ti 数据点已经给了负样本）。
- **硬件需求**: 必须 CUDA 卡长期可用。
- **适配度**: 中低——C++/CUDA 不是你的栈，但模块边界清晰（决策函数+遥测可以先用 Python 原型验证再翻译）。

### M4 · 三层完整版: slotstream ExpertStore 的 CUDA 忠实移植 【真·空位，工程量大】
- **是什么**: 现有全部方案只做 RAM→VRAM 两层。NVMe 层只有: slotstream（Mac）、Lidenburg 的 SATA SSD 提及、vLLM PR3 的 "mmap disk tier"（未动工）。用 io_uring + O_DIRECT + 定长 expert record（你 maple 移植时已吃透的 9-piece 布局）做 NVMe→RAM→VRAM，加上 Governor 式 15s 弹性伸缩。
- **目标场景**: 5×VRAM 级巨型 MoE（Flash-Next 105GB @ 12GB 卡）——llama.cpp RFC 的数据（缓存吃掉带宽墙）预示这层的边际收益在 NVMe>3GB/s 的机器上可观。
- **工作量**: 月级; 是 "第二个引擎" 的活，适合作为 M1/M2 出数据之后的进阶项，或论文的 systems 章节而不是第一个 commit。

### M5 · MoEpic 式专家纵切 × slotstream record 布局 【研究型，可发论文】
- **是什么**: MoEpic 证明纵向切分能在同 VRAM 下多存一倍专家; slotstream 的定长 record + row_alpha 布局天然支持"上半段常驻+下半段流式"的变体。两者没人结合过。
- **形式**: 模拟器扩展（M1 的 harness 直接复用）→ 若模拟器收益>15% 再上真机。
- **风险**: 切分破坏 pread 定位简洁性; 与 MXFP4 group 边界交互需要重新设计。

### M6 · miss 分级处理（SPICE 的 LoRE 替代 + Fiddler 的 CPU 决策统一）【长线】
- SPICE 的置信度门控 + CPU 异步残差是 2026-08 最新的 miss 处理 SOTA; 把它的决策逻辑做成可插拔策略进 M1 harness，同样没人做公开对比。

---

## 4. 建议路线（对你: eval automation + pipeline engineering 的技能栈）

1. **本周可做（零硬件）**: 写 M1 的回放模拟器 v0——输入: 你 slotstream 的 router-trace JSON + ShareGPT 分布的公开 trace; 策略: LRU / LFU / 频率 top-k / top-1 persistence; 输出: 命中率曲线 + 按 PCIe 带宽参数化 t/s 投影。这个 artifact 本身就是 M2/M3 谈判筹码和论文 Table 1。
2. **两周内**: 用 autodl 租 4090 跑 llama.cpp `--cpu-moe` 基线 + leloch `moe-cache-pr` 分支 A/B（复现 batot1 的 sweep 方法论，gpt-oss-20b 或 DeepSeek-V2-Lite），把模拟器预测 vs 真机 hit rate 对齐——这一步让 M1 从"纸上策略"变成"已校准的预测器"。
3. **一个月内**: 按 2 的校准结果，选 M2（vLLM/tinyserve Python PR）或 M3（llama.cpp 修复）落地第一个开源模块。
4. **论文路径**: M1(+M5) 组成 "temporal-locality expert caching for consumer GPUs" 的完整故事: 你已有 Mac 侧观察（Ch07）→ N 卡侧方法 → 公开 harness → 真机校准。CASIA 硕士期间可完成。

## 5. 风险与诚实提醒

- 你 Ch07 的 temporal 数据来自 lab 固定 prompt; **wild 分布下 persistence 可能显著衰减**（2608.12103 已证明 domain shift 会摧毁静态表: 74%→21-34%）。M1 第一步必须先在 wild trace 上重测 persistence——如果衰减到 <1.5× random，M1 的故事就不成立，及时止损转 M2（LRU 增量改进仍然可做）。
- llama.cpp RFC 多 fork 并存 = 概念已验证 = 纯"复现"没有发表价值; 你的增量必须落在预测器/策略/三层，不在缓存本体。
- vLLM PR1 若被拒或重构，M2 的接口假设会变; 提交前重读 RFC 状态。
