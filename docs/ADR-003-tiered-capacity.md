# ADR-003 · 三档模型供给/需求模型 (Tiered Capacity & Graceful Degradation)

状态: 提案待审 · 日期: 2026-09-10 · 作者: 用户构想 + Hermes 细化
取代: DESIGN.md 风险项#1 的"单模型冗余"策略; 为 v0.2 的容量调度定调

---

## 1. 核心理念 (用户原话的结构化)

供给端 = 节点池; 需求端 = 推理请求。系统维护 **高/中/低 三个能力档位**:

- **调用方永远只请求一个模型名** (如 `qwen3:14b`, 即 high 档) — 不感知档位、不感知机器;
- **默认落 high 档**: 小模型本身不构成资源浪费, 高能力是默认体验;
- **拉满才扩容**: high 档所在节点并发饱和 → 让其他**满足容量条件**的空闲机器加载同一模型 (横向扩容);
- **无机器可扩 → 降级链**: high 不可用 → mid → low。调用方只感知"这次回复变笨了一点", 不感知"换了一台机器跑了个小模型"。

**这个方案优于最初的"请求方声明 tier 标签"草案**: 调用方 API 零改动 (纯 OpenAI 兼容), 降级是系统内部的质量弹性而非接口负担。

## 2. 概念定义

| 术语 | 定义 |
|---|---|
| **Tier (档位)** | 能力等级: high/mid/low。每个 tier 绑定一个具体模型 (配置声明) |
| **Node capacity** | 节点可承载的最大模型体积 = 可用内存/显存 - 系统预留 (~4GB) |
| **稳态分布** | 无请求压力时各节点驻留的模型集合 |
| **扩容 (scale-out)** | 让一台**未驻留某 tier 模型但容量满足**的空闲节点加载该模型 |
| **降级链 (degradation chain)** | high → mid → low 的替代顺序, 只在扩容无候选时触发 |
| **滞后 (hysteresis)** | 扩容/缩容/降级的防抖规则, 避免模型频繁加载卸载 (冷加载 20s+ 是反模式) |

## 3. 供给端稳态策略 (每个节点默认驻留什么)

**原则: 默认装 high; 装不下 high 才退而装 mid/low; 空闲时间可叠加装载。**

```
节点加入时 (register):
    capacity = 可用内存 - 4GB 预留
    if capacity ≥ high.size:  驻留 high            # 14B = 9.3GB, 16GB 机器 OK
    elif capacity ≥ mid.size: 驻留 mid
    elif capacity ≥ low.size: 驻留 low
    else: 不参与 (上报 capacity=0)
```

实例 (当前双机):
| 节点 | 可用容量 | 稳态驻留 | 备注 |
|---|---|---|---|
| mac-air M5 (10GB 可用) | ≥9.3 | **high (qwen3:14b)** | 48 t/s, 主力 |
| win-laptop MX450 (10GB) | ≥9.3 | **high (qwen3:14b)** | 5 t/s, 慢但可扩容 |

未来出现大机器 (如 48GB Mac mini, 40GB 可用): 稳态 = high + **quality (32B)** — quality tier 在配置里声明但只有大机器能供给。

**夜间/空闲叠加**: 节点 `idle=true` 且容量有余 → 可同时驻留 mid 或 low 作为"降级热备" (省去降级时的冷加载)。例如 mac-air 夜间 = 14b + 4b 双驻留 (14.3GB > 10GB 装不下 → 只驻留 14b, low 热备给 MX450); 此为示例, 实际由容量约束自动决定。

## 4. 需求端调度状态机 (每请求的决策树)

```
请求到达 (model = high):
  ① 找已驻留 high 且 alive 的节点
     → 有: L1/L-Prod 选一个 (pending 最少 / 延迟感知) → 转发
  ② 无 (全忙/全离线): 触发扩容
     候选 = alive & capacity ≥ high.size & 未驻留 high & idle 信号正常
     → 有: 下发 load_model(high) → 加载完成 → 转发 (首次请求吃冷加载延迟)
     → 无: 进入降级链
  ③ 降级链: mid 驻留节点? → 无则扩容 mid → 无则 low 同理
     降级请求打上 trace 标记 (degraded=true) → 响应头 X-Jiesuan-Tier: mid
  ④ 全链不可用: 503 + 明确错误信息
```

### 4.1 触发参数 (v0.2 初值, 需实测调参)

| 参数 | 初值 | 说明 |
|---|---|---|
| scale_out_pending | 2 | 节点 pending > 2 视为饱和 |
| scale_out_window | 5s | 饱和状态持续 5s 才触发 (瞬时尖峰不扩容) |
| cooldown_load | 30min | 新加载模型至少驻留 30min 才允许卸载 |
| scale_in_util | 20% | 扩容后窗口内利用率 < 20% 触发缩容评估 |
| idle_grace | 120s | 节点 idle=true 后需稳定 2min 才可被选为扩容目标 (防止用户刚离开就装模型) |

## 5. 用户占用与弹性回收 (办公场景的生死线)

**铁律: 机器的主人永远优先于池子。**

- 节点 agent 检测用户活动 (键盘/鼠标/前台应用): `idle=false` 立即上报;
- 网关收到 `idle=false` → 90 秒宽限期 (给正在跑的请求收尾) → 下发 unload 全部模型 → 节点标记 `capacity=0` (不参与调度);
- 节点不执行卸载指令的兜底: 网关 3 次未收到 ack → 将节点标记为不健康并摘流 (宁可少一个节点, 不能抢主人的机器);
- `idle=true` 恢复 → 节点回到稳态分布 (重新按 §3 加载)。

## 6. 与 LiteLLM (L-Prod) 的关系

- 扩容/降级/放置决策 = **借算网关的职责** (新组件 `CapacityManager`);
- 单请求在已定节点间的容错/重试/冷却 = **LiteLLM Router 继续负责** (不变);
- 降级链实现为 LiteLLM 的 fallback 配置由 CapacityManager 动态生成 (mid 可用时 fallback=[mid], 否则 [low]) — 复用其 failover 基建而非重造。

## 7. 验收指标 (v0.2, 全部可实测)

| 指标 | 目标 | 测法 |
|---|---|---|
| 降级透明度 | 调用方 API 零改动 | 同一 client 脚本跑三级模型成功 |
| 扩容延迟 | 空闲节点加载 14B ≤ 30s (含 pull 之外的 load) | trace: load_model 指令→首次响应 |
| 降级正确性 | high 饱和时自动走 mid, 无 503 (除非全链死) | 并发压测 10 并发打 2 台池 |
| 回收正确性 | idle=false 后 90s 内节点退出调度 | 手动动鼠标观察 /nodes |
| 防抖动 | 30min 内模型加载/卸载 ≤ 2 次/节点 | trace 统计 |
| 体验 | Mac 高峰期吞吐不因池子引入劣化 | 压测对比: 扩容前后 Mac 侧 t/s |

## 8. 实现拆解 (预计 3-4 天)

| 项 | 改动 |
|---|---|
| 心跳协议 | +idle, +load, +capacity (Node agent ~30 行) |
| Tier 配置 | 网关 `--tier high=qwen3:14b --tier mid=qwen3:8b --tier low=qwen3:4b` |
| CapacityManager | 新组件: 状态机 + 触发器 + 放置执行器 (~200 行) |
| 控制通道 | 心跳响应扩展指令: load_model/unload_model (节点执行 ollama pull/create 已有, 只差 stop) |
| 降级路由 | chat_completions 决策树重写 (~80 行) |
| trace | +tier, +degraded, +scale_event 字段 |

## 9. 开放问题 (审阅时拍板)

1. **tier 命名**: high/mid/low 内部叫法 OK? 对外是否隐藏 (只暴露模型名)?
2. **mid/low 的默认模型**: 8b/4b 还是同家族其他 (qwen3:8b vs deepseek-r1:8b)? 跨家族降级质量影响?
3. **请求显式指定模型时** (`model: qwen3:4b`) 是否绕过降级链 (严格模式)? → 建议默认严格, 请求头 `X-Jiesuan-Elatic: true` 才允许降级
4. **capacity 上报的可信度**: agent 自报 vs 网关探测? v0.2 用自报 (oss 占用 = ollama ps), v0.3 网关复核
