# ADR-003 补充 · 业界现状对标: 谁做过类似的"档位调度"?

日期: 2026-09-10 · 结论先行: **LiteLLM 已提供 ADR-003 需要的全部底层原语, 我们不重造; 但"档位与节点容量/可用性联动"的完整闭环在开源界仍是空白 — 这是借算的增量, 不是重复造轮子。**

---

## 1. LiteLLM 已内建的相关能力 (直接可用, 不重造)

调研官方文档 (docs.litellm.ai/routing, /proxy/fallback_management, /config_settings) 后确认:

| 能力 | LiteLLM 现状 | 对 ADR-003 的意义 |
|---|---|---|
| **Fallback 链** | ✅ `Router(fallbacks=[...])`: high 不可用→自动切 mid/low; 支持 `context_window_fallbacks`/`content_policy_fallbacks` 分类触发 | 降级链**不用自己写** — CapacityManager 只需动态更新 fallback 配置 |
| **Fallback 管理 API** | ✅ `POST /fallback` 可**运行时动态增删** fallback 链 (无需重启) | 扩容/缩容时动态改降级链 = 改配置即可 |
| **Cooldown** | ✅ 失败节点自动冷却 (allowed_fails + cooldown_time) | 僵死节点摘流已验证 |
| **重试** | ✅ num_retries + 指数退避 | 已用 |
| **延迟感知路由** | ✅ latency-based-routing | 已用 |
| **队列/优先级** | ✅ "Prioritizing important requests (Queueing)" | 饱和时的排队语义可用 |
| **跨实例状态** | ✅ Redis 共享 cooldown/用量 (多网关部署时) | 将来多网关扩展用 |
| **每 deployment 约束** | ✅ rpm/tpm 限制 per deployment | 可以模拟"节点容量上限" |

### 映射: ADR-003 概念 → LiteLLM 原语

```
降级链 high→mid→low     = Router(fallbacks=[{"qwen3:14b": ["qwen3:8b", "qwen3:4b"]}])
扩容(让空闲节点装模型)   = 无对应 ← ★ 这是必须自己写的部分
节点容量/可用性信号      = 无对应 ← ★ 也要自己写 (litellm 只有 rpm/tpm, 没有 RAM/idle)
放置状态机(加载/卸载/滞后) = 无对应 ← ★ 自己写
```

**修正 ADR-003 §6 的实现策略**: 降级链不"动态生成 fallback 配置"那么绕 — LiteLLM 原生支持静态声明 `fallbacks=[{"qwen3:14b": ["qwen3:8b", "qwen3:4b"]}]`, 且**只有当 qwen3:8b/4b 的 deployment 存在时才会被路由到**。所以降级链天然"按需生效": mid 模型没被加载到任何节点 → deployment 列表里没有 → fallback 自动跳过 → 继续降。**CapacityManager 只负责"让 deployment 出现/消失"(即模型加载/卸载), 降级语义由 LiteLLM 免费获得。**

## 2. 学术界: 相邻工作两篇 (2026-06)

### RouteBalance (arXiv 2606.17949) — 最接近的学术先行者
- 问题定义几乎同构: "model routers 按 quality/cost 选模型但**忽略实例负载**, load balancer 优化队列但**忽略质量**" — 两层割裂。提出 fused 调度: 单次在线决策同时权衡 **quality × latency × cost**, 在 13 实例 28 GPU 异构集群上四档模型服务。
- 结果: 决策质量 +0.013 (DeepEval), 高负载下领先增强版基线 2.6-4.1×; 热路径开销 ~32ms @ 12req/s。
- **与借算的差异**: RouteBalance 假设所有模型实例常驻数据中心 GPU (容量恒定); 借算的核心新约束是**节点是别人的办公电脑** (容量时变 + 随时退出 + 空闲才可用)。它的 "fused 决策" 思想可以在 v1.0 借鉴, 但"capacity-aware placement on volunteered devices" 没有先例。
- 引用价值: related work 必引; 其"两层割裂"的问题陈述直接为借算的动机背书。

### Cluster, Route, Escalate (arXiv 2606.27457)
- 两阶段级联: 先聚类查询分配给性价比最优的模型, 预算内再 escalate 到大模型。
- 与借算的降级链**方向相反** (它是 cost-aware 升级, 我们是 capacity-aware 降级), 但"级联+预算"的框架可参考。

### 其他生产实践
- "AI Gateway 四种路由策略" 类博文 (buildmvpfast/akshayghalme 2026): 业界共识是 fallback+cooldown+priority 是 gateway 标配, **但没有一家把"节点是闲置办公设备"作为一等公民** — 她们的 deployment 都是稳定的机房实例。

## 3. 对 ADR-003 的修订 (3 处)

1. **§6 降级链实现**: 从"动态生成 fallback"改为"静态声明 fallback + deployment 按需出现/消失自动生效" (更简单, 详见 §1 映射);
2. **§8 实现拆解**: CapacityManager 减负 — 不再自己实现降级路由决策树 (~80行删掉), 只做"容量→deployment 出现/消失"的转换 (~120 行);
3. **§10 新增 Related Work**: RouteBalance (fused 调度, 数据中心假设) / Cluster-Route-Escalate (级联升级) / PAIR (job-count 调度) — 借算的差异化 = **volunteered edge devices 的容量时变性** + **办公场景的占用回收语义**。

## 4. 空位重申 (写论文时的 positioning)

| 维度 | LiteLLM | RouteBalance | **借算** |
|---|---|---|---|
| 节点性质 | 稳定 API 端点 | 稳定机房 GPU | **时变可用办公设备** |
| 容量模型 | rpm/tpm 配额 | 恒定 | **RAM/显存 + idle 状态** |
| 质量弹性 | fallback 链 | fused 决策 | **容量驱动的档位缩放** |
| 占用语义 | 无 | 无 | **主人优先/强制回收** |
| 部署假设 | 中心 proxy | 数据中心 | **内网/边缘, 零 agent 可选** |

结论: 组件级(降级/路由/冷却)全是成熟品, **系统级(时变容量 volunteered devices + 办公语义)的组合是新的** — 这正是"组合创新"的正当性: 不发明轮子, 发明轮子的排布方式。
