"""LiteLLM Router 调度后端 (L-Prod)。

定位: 生产执行后端 — 把节点池映射成 LiteLLM Router 的部署列表,
获得 延迟感知路由 / 失败冷却 / 自动 failover / 重试。研究层 L0/L1/L2 不受影响。

为什么选 LiteLLM (对比 PAIR/llm-d/mycoSwarm, 2026-09):
  - 唯一可库级嵌入的框架(纯 Python), 不需要独立路由进程
  - 自带 latency-based routing (补 PAIR 自认的"不看容量/延迟"缺陷)
  - cooldown + failover + retry = 借算 v0.1 尚未实现的容错面
  - 原生支持 Ollama 的 OpenAI 兼容端点 (异构 Mac/NVIDIA 直接可用)
  - llm-d 更强但 K8s-only; mycoSwarm 太早期; PAIR 不可嵌入且策略更弱
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from jiesuan_gateway.__main__ import NodeEntry, NodeTable, Scheduler


@dataclass
class LiteLLMScheduler(Scheduler):
    """把节点池桥接到 LiteLLM Router。

    model_to_deployments: {模型名: [deployment_name, ...]} — 由节点表周期刷新。
    路由策略: latency-based (ROUTING) + cooldown-on-fail。
    """
    name = "L-Prod-litellm"

    def __post_init__(self) -> None:
        self._router = None
        self._deployments: dict[str, list[str]] = {}
        self._last_refresh = 0.0
        self._refresh_interval = 15.0

    # ---- 内部: 从节点表构建 LiteLLM deployments ----------------------------

    def _refresh(self, nodes_table: NodeTable) -> None:
        from litellm import Router

        now = time.time()
        if now - self._last_refresh < self._refresh_interval:
            return
        self._last_refresh = now

        by_model: dict[str, list[dict]] = {}
        for n in nodes_table.alive():
            # engine_base_url: agent节点=代理端点 / engine哑节点=引擎直连
            base = (n.proxy_url if n.kind == "engine"
                    else n.proxy_url)  # v0.2: agent节点也可走 /v1 代理
            for m in n.models:
                dep_name = f"jiesuan-{n.name}"
                by_model.setdefault(m, []).append({
                    "model_name": m,                       # 对外暴露的模型名
                    "litellm_params": {
                        "model": f"ollama/{m}",            # litellm 的 ollama provider
                        "api_base": base,                  # 节点引擎直连
                    },
                    "model_info": {"id": dep_name},
                })

        # 只有部署列表变化时才重建 Router (Router 构建开销大)
        new_map = {k: [d["model_info"]["id"] for d in v] for k, v in by_model.items()}
        if new_map != self._deployments or self._router is None:
            all_deps = [d for deps in by_model.values() for d in deps]
            self._router = Router(
                model_list=all_deps,
                routing_strategy="latency-based-routing",
                num_retries=2,
                retry_after=2,
                cooldown_time=20,          # 失败节点冷却 20s
                allowed_fails=2,
                fallbacks=[],              # 跨模型 fallback 交给上层
                set_verbose=False,
            )
            self._deployments = new_map

    # ---- Scheduler 接口 ---------------------------------------------------

    def pick(self, nodes: list[NodeEntry], model: str, req_meta: dict) -> NodeEntry | None:
        """LiteLLM 模式下 pick() 不做选择 — 网关把请求交给 router.acompletion。

        此方法存在只为兼容 Scheduler ABC; 网关检测到 L-Prod 时走 acompletion 路径。
        返回 None 表示"由 LiteLLM 全权处理"。
        """
        return None

    async def acompletion(self, nodes_table: NodeTable, body: dict):
        """真正入口: 用 LiteLLM Router 执行请求 (非流式)。"""
        self._refresh(nodes_table)
        model = body.get("model", "")
        if self._router is None:
            raise RuntimeError("no deployments available")
        # 去掉 LiteLLM 不认识的字段
        fwd = {k: v for k, v in body.items() if k not in ("stream",)}
        resp = await self._router.acompletion(**fwd, num_retries=2)
        return resp
