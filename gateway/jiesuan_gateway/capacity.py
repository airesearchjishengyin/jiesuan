"""CapacityManager — ADR-003 三档供给/需求模型的容量状态机。

职责:
  1. 追踪节点容量与驻留模型 (capacity/model 驻留表)
  2. 高水位扩容: 饱和 → 选空闲候选 → 下发 load_model
  3. 用户占用回收: idle=false → 宽限 90s → unload → 摘流
  4. 缩容滞后: 驻留 <30min 不卸载; 利用率 <20% 评估缩容

决策的执行通过心跳响应下发指令 (load_model/unload_model);
降级链语义由 LiteLLM fallbacks 提供 (deployment 出现/消失自动生效), 这里不重造。

触发参数见 TUNING。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


# ---------------------------------------------------------------- tuning

@dataclass
class Tuning:
    scale_out_pending: int = 2          # pending > N 视为饱和
    scale_out_window: float = 5.0       # 饱和持续秒数才触发
    idle_grace: float = 120.0           # idle=true 需稳定秒数才可作扩容目标
    busy_grace: float = 90.0            # idle=false 后给正在跑的请求收尾的宽限
    cooldown_load: float = 1800.0       # 新加载模型最少驻留秒数 (30min 防抖)
    scale_in_util: float = 0.20         # 利用率低于此评估缩容
    heartbeat_interval: float = 5.0


TUNING = Tuning()


# ---------------------------------------------------------------- models

@dataclass
class TierConfig:
    """档位声明: high=qwen3:14b mid=qwen3:8b low=qwen3:4b (网关 --tier 参数)。"""
    high: str = ""
    mid: str = ""
    low: str = ""

    def chain(self) -> list[str]:
        """降级链: high → mid → low (跳过空档位)。"""
        return [m for m in (self.high, self.mid, self.low) if m]

    def size_of(self, model: str) -> float:
        """模型体积估算表 (GB, Q4_K_M 级) — v0.2 用静态表, v0.3 从引擎查询。"""
        sizes = {"qwen3:0.6b": 0.7, "qwen3:4b": 3.0, "qwen3:8b": 5.5,
                 "qwen3:14b": 9.3, "qwen3:32b": 20.0}
        return sizes.get(model, 9.5)


@dataclass
class NodeCapacity:
    """单节点的容量状态 (由心跳持续更新)。"""
    name: str
    engine_url: str
    kind: str = "engine"                 # engine | agent
    total_ram_gb: float = 0.0
    used_ram_gb: float = 0.0
    loaded_models: list[str] = field(default_factory=list)   # 引擎实际驻留
    idle: bool = True
    idle_since: float = field(default_factory=time.time)
    busy_since: float = 0.0
    pending: int = 0
    saturated_since: float = 0.0         # 持续饱和起点 (0=未饱和)
    last_loaded_at: dict = field(default_factory=dict)       # model → 加载时间戳
    unload_pending: float = 0.0          # busy 回收宽限截止时间 (0=无)

    def capacity_gb(self, reserve: float = 2.0) -> float:
        return max(0.0, self.total_ram_gb - self.used_ram_gb - reserve)

    def fits(self, model: str, tiers: TierConfig) -> bool:
        return self.capacity_gb() >= tiers.size_of(model)

    def utilization(self) -> float:
        """请求压力指标: pending 归一化 (v0.2 简化: pending/4 上限 1)。"""
        return min(1.0, self.pending / 4.0)


# ---------------------------------------------------------------- manager

class CapacityManager:
    """容量状态机 — 由网关心跳处理循环驱动。"""

    def __init__(self, tiers: TierConfig, gateway_port: int) -> None:
        self.tiers = tiers
        self.gateway_port = gateway_port
        self.nodes: dict[str, NodeCapacity] = {}
        self.pending_commands: dict[str, list[dict]] = {}   # name → [指令] (心跳响应带出)

    # ---- 心跳入口: 更新状态, 返回要下发的指令 ------------------------------

    def on_heartbeat(self, d: dict) -> dict:
        name = d["name"]
        nc = self.nodes.get(name)
        if nc is None:
            nc = NodeCapacity(name=name, engine_url=d.get("engine_url", ""))
            self.nodes[name] = nc
        now = time.time()

        # 更新瞬时字段
        prev_idle = nc.idle
        nc.total_ram_gb = float(d.get("total_ram_gb", 0) or 0)
        nc.used_ram_gb = float(d.get("used_ram_gb", 0) or 0)
        nc.pending = int(d.get("pending", 0))
        nc.idle = bool(d.get("idle", True))
        if nc.idle and not prev_idle:
            nc.idle_since = now
        if not nc.idle and prev_idle:
            nc.busy_since = now

        # 引擎实际驻留模型 (以 ollama ps 为准, 心跳上报)
        engine_models = d.get("loaded_models") or d.get("models") or []
        # 检测外部卸载 (比如 ollama 自动卸载): 更新 last_loaded_at
        for m in list(nc.last_loaded_at):
            if m not in engine_models:
                del nc.last_loaded_at[m]
        nc.loaded_models = engine_models

        # 用户占用回收: idle=false 且还驻留模型 → 宽限后下发卸载
        commands: list[dict] = []
        if not nc.idle and nc.loaded_models:
            if nc.unload_pending == 0.0:
                nc.unload_pending = now + TUNING.busy_grace
            elif now >= nc.unload_pending:
                for m in nc.loaded_models:
                    commands.append({"op": "unload_model", "model": m})
                nc.last_loaded_at.clear()
                nc.unload_pending = 0.0
                print(f"[cap] {name}: user active → unload all", flush=True)
        else:
            nc.unload_pending = 0.0

        # 饱和检测 (高水位)
        if nc.pending > TUNING.scale_out_pending:
            if nc.saturated_since == 0.0:
                nc.saturated_since = now
            elif now - nc.saturated_since >= TUNING.scale_out_window:
                cmds = self._try_scale_out(nc, now)
                commands.extend(cmds)
        else:
            nc.saturated_since = 0.0

        if commands:
            self.pending_commands.setdefault(name, []).extend(commands)

        # 把累积指令带出 (心跳响应体)
        out = self.pending_commands.pop(name, [])
        return {"ok": True, "commands": out}

    # ---- 扩容决策 ---------------------------------------------------------

    def _try_scale_out(self, saturated: NodeCapacity, now: float) -> list[dict]:
        """高水位节点 → 找空闲候选加载同一模型。"""
        commands = []
        for m in saturated.loaded_models:
            candidates = [
                nc for nc in self.nodes.values()
                if (nc.name != saturated.name
                    and nc.idle and nc.idle and (now - nc.idle_since) >= TUNING.idle_grace
                    and m not in nc.loaded_models
                    and nc.fits(m, self.tiers)
                    and all(now - t >= TUNING.cooldown_load for t in
                            [nc.last_loaded_at.get(x, 0) for x in nc.last_loaded_at] or [0]))
            ]
            if candidates:
                # 选容量最大者
                target = max(candidates, key=lambda x: x.capacity_gb())
                commands.append({"op": "load_model", "model": m})
                target.last_loaded_at[m] = now   # 在目标上记账 (指令执行后引擎才有)
                print(f"[cap] scale-out: {m} → {target.name} "
                      f"(triggered by {saturated.name} saturation)", flush=True)
                break  # 每轮每饱和节点只扩一个模型
        return commands

    # ---- 查询: 降级链可用性 (LiteLLM fallbacks 动态映射) --------------------

    def available_tiers(self) -> dict[str, bool]:
        """tier 模型当前是否在池中任意节点驻留。"""
        out = {}
        for tier_model in self.tiers.chain():
            out[tier_model] = any(
                tier_model in nc.loaded_models for nc in self.nodes.values())
        return out

    def fallback_chain(self) -> list[str]:
        """LiteLLM fallbacks: 只包含"当前池内已驻留"的降级目标。"""
        avail = self.available_tiers()
        high = self.tiers.high
        chain = [m for m in self.tiers.chain() if m != high and avail.get(m)]
        return chain
