# ADR-003 附录 · 模型选型决议 (2026-09-10)

> 依据子 agent 调研 (全量 tag 经 ollama.com 官方库当日实抓验证, 交叉印证 Qwen 官方 HF 卡 / Gemma4 技术报告 / Artificial Analysis / MathArena)。
> 完整候选对比见附录末尾"备选"。

## 决议

| 档位 | 模型 | 体积 (Q4) | 发布 | 部署节点 | 预期速度 |
|---|---|---|---|---|---|
| **HIGH** | `qwen3.5:9b` | 6.6GB | 2026-03-02 | mac-air (M5, 10GB 可用) | 60-70 t/s |
| **MID** | `qwen3.5:4b` | 3.4GB | 2026-03-02 | win-rtx (MX450 混合推理) | 10-13 t/s |
| LOW | (v0.3 再定, 候选 `qwen3.5:2b` 1.9GB) | — | — | — | — |

## 推荐理由摘要

- **qwen3.5:9b**: AA 智能指数 32 = <10B 档最强(第二名 16, 领先一倍); GPQA-D 81.7 超过 gpt-oss-120B; 6.6GB 体积在 10GB 预算内留足 KV cache; 原生视觉 + thinking/no_think 混合 + 262K ctx + Apache 2.0。
- **qwen3.5:4b**: AA 指数 27 = <5B 档最强(领先 40%); 3.4GB 权重适配 2GB 显存混合推理; 数学/代码/视觉能力保留。

## 备选 (已评估未采用)

- HIGH 备选 `gemma4:12b` (7.6GB, 2026-06): LiveCodeBench 72 略高、256K ctx — 代码场景优先时可切换。
- MID 备选 `gemma4:e2b-it-qat` (4.3GB QAT): QAT 量化质量更稳, 纯速度优先时切换。

## 排除记录 (防再次踩坑)

- gpt-oss / qwen3 / deepseek-r1 / phi4-reasoning / ministral-3: 发布超 6 个月窗口, 排除;
- **Phi-5 / Qwen4 / Llama 5: 多篇 2026 博客声称存在, ollama.com 实测 404** — 博客信息不可信, 以官方库实抓为准。

## 运维注意

1. qwen3.5 为 thinking 模型: reasoning 时单题可输出 3-5 万 token → 日常问答走 `/no_think` 或限 `num_predict`;
2. 部署顺序: mac-air `ollama pull qwen3.5:9b` → win-rtx `ollama pull qwen3.5:4b` → 网关 `--tier` 参数更新 → 预热各一次。
