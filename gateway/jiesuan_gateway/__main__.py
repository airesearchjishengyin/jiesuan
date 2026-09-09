"""jiesuan-gateway: 借算网关。

职责:
  1. 维护节点表 (注册/心跳超时剔除)
  2. OpenAI 兼容入口 (/v1/chat/completions) — 按 Scheduler 策略路由
  3. 调度器可插拔: L0 RoundRobin (v0.1 默认) / L1 负载感知 / L2 temporal (插槽)

用法:
  python -m jiesuan_gateway --mode pool --port 7800
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import aiohttp
import yarl
from aiohttp import web

HEARTBEAT_TIMEOUT = 20.0  # 秒; 超时未心跳 = 节点下线


# ---------------------------------------------------------------- node table

@dataclass
class NodeEntry:
    name: str
    os: str = ""
    arch: str = ""
    models: list[str] = field(default_factory=list)
    pending: int = 0
    last_seen: float = field(default_factory=time.time)
    proxy_url: str = ""          # http://ip:port (节点代理端点)


class NodeTable:
    """节点注册表 — 线程安全由 asyncio 单线程事件循环保证。"""

    def __init__(self) -> None:
        self._nodes: dict[str, NodeEntry] = {}

    def upsert(self, e: NodeEntry) -> None:
        self._nodes[e.name] = e

    def alive(self, model: str | None = None) -> list[NodeEntry]:
        now = time.time()
        nodes = [n for n in self._nodes.values() if now - n.last_seen <= HEARTBEAT_TIMEOUT]
        if model:
            # 精确匹配或兼容 "name:tag" 前缀匹配
            nodes = [n for n in nodes
                     if model in n.models or any(m.split(":")[0] == model.split(":")[0] for m in n.models)]
        return nodes

    def get(self, name: str) -> NodeEntry | None:
        return self._nodes.get(name)

    def all(self) -> list[NodeEntry]:
        return list(self._nodes.values())


# ---------------------------------------------------------------- schedulers

class Scheduler(ABC):
    """调度器抽象 — L2 temporal 预测器实现此接口即可接入。"""

    name: str = "base"

    @abstractmethod
    def pick(self, nodes: list[NodeEntry], model: str, req_meta: dict) -> NodeEntry | None: ...


class RoundRobin(Scheduler):
    """L0: 轮询 — 无状态, 公平。"""
    name = "L0-roundrobin"

    def __init__(self) -> None:
        self._i = 0

    def pick(self, nodes: list[NodeEntry], model: str, req_meta: dict) -> NodeEntry | None:
        if not nodes:
            return None
        self._i += 1
        return nodes[self._i % len(nodes)]


class LeastPending(Scheduler):
    """L1: 负载感知 — pending 最少优先, 并列时选最近心跳的 (更活)。"""
    name = "L1-leastpending"

    def pick(self, nodes: list[NodeEntry], model: str, req_meta: dict) -> NodeEntry | None:
        if not nodes:
            return None
        return min(nodes, key=lambda n: (n.pending, -n.last_seen))


SCHEDULERS: dict[str, type[Scheduler]] = {
    "L0": RoundRobin,
    "L1": LeastPending,
    # "L2": TemporalScheduler,  ← 学术线插槽, 见 docs/ch09
}


# ---------------------------------------------------------------- gateway app

@dataclass
class GatewayState:
    nodes: NodeTable = field(default_factory=NodeTable)
    scheduler: Scheduler = field(default_factory=RoundRobin)
    trace_path: str = "trace.jsonl"
    req_counter: int = 0


def _trace(st: GatewayState, **kv) -> None:
    """结构化 trace — 与 slotstream router-trace 对齐的 JSONL, 论文实验直接用。"""
    kv["ts"] = time.time()
    try:
        with open(st.trace_path, "a") as f:
            f.write(json.dumps(kv, ensure_ascii=False) + "\n")
    except OSError:
        pass


async def node_register(request: web.Request) -> web.Response:
    st: GatewayState = request.app["state"]
    d = await request.json()
    peer = request.remote or "0.0.0.0"
    e = NodeEntry(
        name=d["name"], os=d.get("os", ""), arch=d.get("arch", ""),
        models=d.get("models", []), pending=d.get("pending", 0),
        last_seen=time.time(),
        proxy_url=f"http://{peer}:{d.get('port', 7801)}",
    )
    st.nodes.upsert(e)
    print(f"[gw] +node {e.name} ({e.os}/{e.arch}) models={e.models} url={e.proxy_url}", flush=True)
    return web.json_response({"ok": True})


async def node_heartbeat(request: web.Request) -> web.Response:
    st: GatewayState = request.app["state"]
    d = await request.json()
    e = st.nodes.get(d.get("name", ""))
    if e is None:
        # 网关重启后节点心跳先到 → 视为重新注册
        return await node_register(request)
    e.last_seen = time.time()
    e.pending = d.get("pending", e.pending)
    e.models = d.get("models", e.models)
    return web.json_response({"ok": True})


async def list_nodes(request: web.Request) -> web.Response:
    st: GatewayState = request.app["state"]
    now = time.time()
    out = [{"name": n.name, "os": n.os, "arch": n.arch, "models": n.models,
            "pending": n.pending, "alive": now - n.last_seen <= HEARTBEAT_TIMEOUT,
            "proxy": n.proxy_url} for n in st.nodes.all()]
    return web.json_response({"scheduler": st.scheduler.name, "nodes": out})


async def chat_completions(request: web.Request) -> web.StreamResponse:
    """OpenAI 兼容入口: 选节点 → 回源代理 → 流式返回。"""
    st: GatewayState = request.app["state"]
    st.req_counter += 1
    rid = st.req_counter
    body = await request.json()
    model = body.get("model", "")

    nodes = st.nodes.alive(model=model)
    node = st.scheduler.pick(nodes, model, {"rid": rid})
    if node is None:
        return web.json_response(
            {"error": f"no alive node serving model '{model}' "
                      f"(alive nodes: {[n.name for n in st.nodes.alive()]})"},
            status=503)

    t0 = time.time()
    _trace(st, rid=rid, event="route", model=model, node=node.name,
           sched=st.scheduler.name, candidates=[n.name for n in nodes])
    try:
        timeout = aiohttp.ClientTimeout(total=None, sock_read=None)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(yarl.URL(f"{node.proxy_url}/v1/chat/completions"),
                              data=json.dumps(body).encode(),
                              headers={"Content-Type": "application/json"}) as up:
                resp = web.StreamResponse(status=up.status,
                                          headers={"Content-Type": up.headers.get(
                                              "Content-Type", "application/json")})
                await resp.prepare(request)
                async for chunk in up.content.iter_any():
                    await resp.write(chunk)
                await resp.write_eof()
                _trace(st, rid=rid, event="done", node=node.name,
                       elapsed=round(time.time() - t0, 3), status=up.status)
                return resp
    except Exception as e:  # noqa: BLE001
        _trace(st, rid=rid, event="error", node=node.name, err=str(e)[:120])
        return web.json_response({"error": f"node {node.name} failed: {e}"}, status=502)


async def sweep_dead_nodes(st: GatewayState) -> None:
    """周期剔除失联节点 (只打印, 数据保留 — 心跳恢复即复活)。"""
    while True:
        await asyncio.sleep(HEARTBEAT_TIMEOUT)
        now = time.time()
        dead = [n.name for n in st.nodes.all() if now - n.last_seen > HEARTBEAT_TIMEOUT]
        if dead:
            print(f"[gw] nodes timing out: {dead}", flush=True)


async def make_app(st: GatewayState) -> web.Application:
    app = web.Application()
    app["state"] = st
    app.router.add_post("/node/register", node_register)
    app.router.add_post("/node/heartbeat", node_heartbeat)
    app.router.add_get("/nodes", list_nodes)
    app.router.add_post("/v1/chat/completions", chat_completions)
    app.router.add_get("/v1/models",
                       lambda r: web.json_response(
                           {"models": sorted({m for n in st.nodes.alive() for m in n.models})}))
    return app


async def amain(args: argparse.Namespace) -> None:
    sched = SCHEDULERS.get(args.scheduler, RoundRobin)()
    st = GatewayState(scheduler=sched, trace_path=args.trace)
    app = await make_app(st)
    sw = asyncio.create_task(sweep_dead_nodes(st))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", args.port)
    await site.start()
    print(f"[gw] jiesuan gateway '{sched.name}' on :{args.port} (mode={args.mode}, "
          f"trace={args.trace})", flush=True)
    with contextlib.suppress(asyncio.CancelledError):
        await sw
        await asyncio.Event().wait()


def main() -> None:
    ap = argparse.ArgumentParser("jiesuan-gateway")
    ap.add_argument("--mode", choices=["solo", "pool", "network"], default="pool")
    ap.add_argument("--port", type=int, default=7800)
    ap.add_argument("--scheduler", choices=list(SCHEDULERS), default="L1",
                    help="L0=roundrobin L1=leastpending")
    ap.add_argument("--trace", default="trace.jsonl")
    args = ap.parse_args()
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("[gw] bye", flush=True)


if __name__ == "__main__":
    main()
