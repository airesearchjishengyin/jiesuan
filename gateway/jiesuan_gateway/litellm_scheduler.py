"""LiteLLM Router 调度后端 (L-Prod)。

定位: 生产执行后端 — 把节点池映射成 LiteLLM Router 的部署列表,
获得 延迟感知路由 / 失败冷却 / 自动 failover / 重试。研究层 L0/L1/L2 不受影响。

Qwen3.5 thinking-model handling: content 为空时回填 reasoning_content (ADR-003 附录),
并对 qwen3.5* 自动注入 /no_think (日常问答不需要 3-5万 token 的 reasoning)。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from jiesuan_gateway.__main__ import NodeEntry, NodeTable, Scheduler

THINKING_PREFIX = "/no_think"  # qwen3.5 系列: 系统提示注入, 避免推理模式烧 token


def _is_thinking_model(model: str) -> bool:
    return model.startswith("qwen3.5")


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

    def _refresh(self, nodes_table: NodeTable, capacity=None, tiers=None) -> None:
        from litellm import Router

        now = time.time()
        if now - self._last_refresh < self._refresh_interval:
            return
        self._last_refresh = now

        by_model: dict[str, list[dict]] = {}
        for n in nodes_table.alive():
            # engine_base_url: agent节点=代理端点 / engine哑节点=引擎直连
            base = n.proxy_url
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
            # ADR-003: tier 降级链 — high 饱和/不存在时 fallback 到 mid/low
            # (deployment 不存在时 litellm 自动跳过该 fallback, 降级语义按需生效)
            fallbacks: list[dict] = []
            if tiers and tiers.high:
                chain = [m for m in tiers.chain() if m != tiers.high]
                if chain:
                    fallbacks = [{tiers.high: chain}]
            self._router = Router(
                model_list=all_deps,
                routing_strategy="latency-based-routing",
                num_retries=2,
                retry_after=2,
                cooldown_time=20,          # 失败节点冷却 20s
                allowed_fails=2,
                fallbacks=fallbacks,
                set_verbose=False,
            )
            self._deployments = new_map
            if fallbacks:
                print(f"[lprod] fallback chain: {fallbacks}", flush=True)

    # ---- Scheduler 接口 ---------------------------------------------------

    def pick(self, nodes: list[NodeEntry], model: str, req_meta: dict) -> NodeEntry | None:
        """LiteLLM 模式下 pick() 不做选择 — 网关把请求交给 router.acompletion。

        此方法存在只为兼容 Scheduler ABC; 网关检测到 L-Prod 时走 acompletion 路径。
        返回 None 表示"由 LiteLLM 全权处理"。
        """
        return None

    async def astream(self, nodes_table: NodeTable, body: dict, capacity=None, tiers=None):
        """流式入口: Router.astream (qwen3.5 /no_think 注入同 acompletion)。"""
        self._refresh(nodes_table, capacity, tiers)
        model = body.get("model", "")
        if self._router is None:
            raise RuntimeError("no deployments available")
        fwd = {k: v for k, v in body.items() if k != "stream"}
        if _is_thinking_model(model):
            msgs = fwd.get("messages") or []
            if msgs and not any(
                isinstance(m.get("content"), str) and "/no_think" in m.get("content", "")
                for m in msgs):
                injected = dict(msgs[0])
                injected["content"] = f"{THINKING_PREFIX} {injected.get('content', '')}"
                fwd["messages"] = [injected] + msgs[1:]
        return await self._router.acompletion(**fwd, stream=True, num_retries=2)

    async def acompletion(self, nodes_table: NodeTable, body: dict, capacity=None, tiers=None):
        """真正入口: 用 LiteLLM Router 执行请求 (非流式)。"""
        self._refresh(nodes_table, capacity, tiers)
        model = body.get("model", "")
        if self._router is None:
            raise RuntimeError("no deployments available")
        fwd = {k: v for k, v in body.items() if k != "stream"}
        # qwen3.5 thinking 模型: 默认 /no_think (用户消息级软开关, 已验证)
        if _is_thinking_model(model):
            msgs = fwd.get("messages") or []
            if msgs and not any(
                isinstance(m.get("content"), str) and "/no_think" in m.get("content", "")
                for m in msgs):
                injected = dict(msgs[0])
                injected["content"] = f"{THINKING_PREFIX} {injected.get('content', '')}"
                fwd["messages"] = [injected] + msgs[1:]
        resp = await self._router.acompletion(**fwd, num_retries=2)
        # thinking 模型 content 为空时回填 reasoning (调用方拿到的永远是可用文本)
        try:
            msg = resp.choices[0].message
            if not (msg.content or "").strip():
                reasoning = getattr(msg, "reasoning_content", None) or ""
                if reasoning:
                    msg.content = reasoning
        except (AttributeError, IndexError):
            pass
        return resp
