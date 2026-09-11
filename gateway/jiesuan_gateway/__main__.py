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
import os
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import aiohttp
import yarl
from aiohttp import web

from jiesuan_gateway.capacity import CapacityManager, TierConfig

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
    kind: str = "agent"          # agent=有jiesuan-node代理 / engine=哑节点直连Ollama
    transport: str = "push"      # push=网关主动POST节点 / ws=节点pull长连接 (穿NAT)

    def chat_url(self) -> str:
        """回源地址: agent节点走 /v1/chat/completions 代理; 哑节点直连 Ollama /api/chat。"""
        if self.kind == "engine":
            return f"{self.proxy_url}/api/chat"
        return f"{self.proxy_url}/v1/chat/completions"


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
            # 匹配优先级: 精确 > 节点有同名无tag基础名。不同tag (qwen3:4b vs qwen3:14b) 不互配!
            base = model.split(":")[0]
            exact = [n for n in nodes if model in n.models]
            if exact:
                nodes = exact
            else:
                nodes = [n for n in nodes if base in n.models]
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
    """L1: 负载感知 — pending 最少优先, 并列时随机 (避免心跳 newest 偏置)。
    RTT 感知 (EWMA × pending) 是 v0.4 跨网调度目标, 见 docs/ch09。"""
    name = "L1-leastpending"

    def pick(self, nodes: list[NodeEntry], model: str, req_meta: dict) -> NodeEntry | None:
        if not nodes:
            return None
        best = min(n.pending for n in nodes)
        return random.choice([n for n in nodes if n.pending == best])


SCHEDULERS: dict[str, type[Scheduler]] = {
    "L0": RoundRobin,
    "L1": LeastPending,
    # "L2": TemporalScheduler,  ← 学术线插槽, 见 docs/ch09
}


def _get_scheduler(name: str) -> Scheduler:
    if name == "litellm":
        from jiesuan_gateway.litellm_scheduler import LiteLLMScheduler
        return LiteLLMScheduler()
    return SCHEDULERS.get(name, RoundRobin)()


# ---------------------------------------------------------------- gateway app

@dataclass
class GatewayState:
    nodes: NodeTable = field(default_factory=NodeTable)
    scheduler: Scheduler = field(default_factory=RoundRobin)
    trace_path: str = "trace.jsonl"
    req_counter: int = 0
    static_nodes: list[dict] = field(default_factory=list)  # 无agent哑节点: [{"name","url","models":[...]}]
    tiers: "TierConfig | None" = None
    capacity: "CapacityManager | None" = None
    manually_offline: set = field(default_factory=set)  # 手动摘流的节点名 (admin 控制)
    pull_conns: dict = field(default_factory=dict)      # name → ws (pull 节点的长连接)
    pending_ws: dict = field(default_factory=dict)      # rid → Future (等待 pull 节点回包)
    node_token: str = ""    # 公网模式: 节点注册/心跳鉴权 (空=不鉴权, 仅限内网)
    api_key: str = ""       # 公网模式: /v1 数据面 Bearer 鉴权 (空=不鉴权)

    def bootstrap_static(self) -> None:
        """把 --static-node 配置注册进节点表 (哑节点: 引擎直连, 无心跳, 常驻)。"""
        for s in self.static_nodes:
            entry = NodeEntry(name=s["name"], proxy_url=s["url"].rstrip("/"),
                              models=s.get("models", []), os=s.get("os", "engine"),
                              kind="engine",
                              last_seen=time.time() + 1e9)  # 永不过期
            self.nodes.upsert(entry)
            print(f"[gw] +static-node {entry.name} → {entry.proxy_url} models={entry.models}",
                  flush=True)


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

    # CapacityManager: 容量/idle 状态机 (返回要下发的指令)
    commands: list[dict] = []
    if st.capacity is not None:
        d2 = dict(d)
        d2["engine_url"] = e.proxy_url
        cap_res = st.capacity.on_heartbeat(d2)
        commands = cap_res.get("commands", [])
    return web.json_response({"ok": True, "commands": commands})


async def list_nodes(request: web.Request) -> web.Response:
    st: GatewayState = request.app["state"]
    now = time.time()
    out = [{"name": n.name, "os": n.os, "arch": n.arch, "models": n.models,
            "pending": n.pending, "alive": now - n.last_seen <= HEARTBEAT_TIMEOUT,
            "proxy": n.proxy_url, "transport": n.transport} for n in st.nodes.all()]
    return web.json_response({"scheduler": st.scheduler.name, "nodes": out})


# ---------------------------------------------------------------- pull mode (WS)

async def node_pull(request: web.Request) -> web.WebSocketResponse:
    """节点 pull 入口: 节点出站 WS 挂进来, 网关沿连接下发请求 (穿 NAT)。

    公网模式 (--node-token): hello 消息必须携带正确 token, 否则拒绝 (防假节点)。
    """
    st: GatewayState = request.app["state"]
    ws = web.WebSocketResponse(heartbeat=15)
    await ws.prepare(request)
    name: str | None = None

    async for msg in ws:
        if msg.type != aiohttp.WSMsgType.TEXT:
            break
        try:
            d = json.loads(msg.data)
        except Exception:  # noqa: BLE001
            continue
        mtype = d.get("type")

        if mtype == "register":
            if st.node_token and d.get("token") != st.node_token:
                print("[gw] ✗ pull 注册被拒: token 无效", flush=True)
                await ws.close(code=4001, message=b"bad token")
                break
            name = str(d["name"])
            e = NodeEntry(
                name=name, os=d.get("os", ""), arch=d.get("arch", ""),
                models=d.get("models", []), pending=d.get("pending", 0),
                last_seen=time.time(), proxy_url=f"ws://pull-conn/{name}",
                transport="ws",
            )
            old = st.pull_conns.get(name)
            if old is not None and old is not ws:
                await old.close(code=4000, message=b"replaced by new connection")
            st.pull_conns[name] = ws
            st.nodes.upsert(e)
            print(f"[gw] +pull-node {e.name} ({e.os}/{e.arch}) models={e.models} "
                  f"via outbound ws", flush=True)

        elif mtype == "heartbeat":
            if name is None:
                continue
            e = st.nodes.get(name)
            if e is not None:
                e.last_seen = time.time()
                e.pending = d.get("pending", e.pending)
                e.models = d.get("models", e.models)
                # CapacityManager 指令经 WS 下发
                if st.capacity is not None:
                    d2 = dict(d)
                    d2["engine_url"] = e.proxy_url
                    cap_res = st.capacity.on_heartbeat(d2)
                    for c in cap_res.get("commands", []):
                        await ws.send_json({"op": "command", "command": c})

        elif mtype == "result" or d.get("op") == "result":
            fut = st.pending_ws.pop(d.get("rid"), None)
            if fut is not None and not fut.done():
                fut.set_result(d)

    # 连接断开 → 节点立即失效 (对比 push 要等心跳超时)
    if name is not None and st.pull_conns.get(name) is ws:
        st.pull_conns.pop(name, None)
        e = st.nodes.get(name)
        if e is not None:
            e.last_seen = 0.0
        print(f"[gw] -pull-node {name} (ws closed)", flush=True)
    return ws


async def dispatch_ws(st: GatewayState, node: NodeEntry, rid: int,
                      body: dict) -> tuple[int, bytes] | None:
    """经 pull 隧道下发一个 chat 请求并等待完整结果。返回 (status, body_bytes)。"""
    ws = st.pull_conns.get(node.name)
    if ws is None or ws.closed:
        return None
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    st.pending_ws[rid] = fut
    try:
        await ws.send_json({"op": "chat", "rid": rid, "body": body})
        result = await asyncio.wait_for(fut, timeout=600)
    except asyncio.TimeoutError:
        st.pending_ws.pop(rid, None)
        return None
    except Exception:
        st.pending_ws.pop(rid, None)
        return None
    payload = result.get("payload", "")
    if "error" in payload[:40] and payload.startswith("{\"error\""):
        return 502, payload.encode()
    return 200, payload.encode()


async def chat_completions(request: web.Request) -> web.StreamResponse:
    """OpenAI 兼容入口: 选节点 → 回源代理 → 流式返回。"""
    st: GatewayState = request.app["state"]
    # 数据面鉴权 (--api-key):
    #   - 本机回环 (Hermes /model js 等) 且非经 CF 隧道 → 信任放行, 免 Bearer
    #   - 经 CF Tunnel 的公网请求 (带 CF-Connecting-IP) 或非回环来源 → 必须 Bearer
    if st.api_key:
        peer = request.remote or ""
        trusted_local = (peer in ("127.0.0.1", "::1", "::ffff:127.0.0.1")
                         and "CF-Connecting-IP" not in request.headers)
        if not trusted_local and request.headers.get("Authorization") != f"Bearer {st.api_key}":
            return web.json_response({"error": "unauthorized"}, status=401)
    st.req_counter += 1
    rid = st.req_counter
    body = await request.json()
    model = body.get("model", "")

    nodes = st.nodes.alive(model=model)
    # 手动摘流的节点不参与调度
    nodes = [n for n in nodes if n.name not in st.manually_offline]
    node = st.scheduler.pick(nodes, model, {"rid": rid})
    t0 = time.time()

    # ---- L-Prod (LiteLLM) 路径: 路由/failover/重试全权交给 Router ----
    if node is None and st.scheduler.name.startswith("L-Prod"):
        wants_stream = bool(body.get("stream"))
        try:
            if wants_stream:
                # 流式: Router.astream → SSE 透传
                stream = await st.scheduler.astream(st.nodes, body, st.capacity, st.tiers)  # type: ignore[union-attr]
                resp = web.StreamResponse(status=200,
                                          headers={"Content-Type": "text/event-stream",
                                                   "Cache-Control": "no-cache"})
                await resp.prepare(request)
                async for chunk in stream:
                    piece = chunk.model_dump_json() if hasattr(chunk, "model_dump_json") else json.dumps(chunk)
                    await resp.write(f"data: {piece}\n\n".encode())
                await resp.write(b"data: [DONE]\n\n")
                await resp.write_eof()
                _trace(st, rid=rid, event="done", node="litellm-router",
                       elapsed=round(time.time() - t0, 3), status=200, stream=True)
                return resp
            resp = await st.scheduler.acompletion(st.nodes, body, st.capacity, st.tiers)  # type: ignore[union-attr]
            _trace(st, rid=rid, event="done", node="litellm-router",
                   elapsed=round(time.time() - t0, 3), status=200)
            payload = resp.model_dump() if hasattr(resp, "model_dump") else dict(resp)
            return web.json_response(payload)
        except Exception as e:  # noqa: BLE001
            _trace(st, rid=rid, event="error", node="litellm-router", err=str(e)[:160])
            return web.json_response({"error": f"litellm router: {e}"}, status=502)

    if node is None:
        return web.json_response(
            {"error": f"no alive node serving model '{model}' "
                      f"(alive nodes: {[n.name for n in st.nodes.alive()]})"},
            status=503)

    t0 = time.time()
    _trace(st, rid=rid, event="route", model=model, node=node.name,
           sched=st.scheduler.name, candidates=[n.name for n in nodes])
    try:
        # ---- pull (ws) 节点: 经出站隧道下发, 无需节点可达 ----
        if node.transport == "ws":
            res = await dispatch_ws(st, node, rid, body)
            if res is None:
                _trace(st, rid=rid, event="error", node=node.name, err="ws tunnel gone")
                return web.json_response({"error": f"node {node.name} tunnel lost"}, status=502)
            status, payload = res
            return web.Response(status=status, body=payload,
                                content_type="application/x-ndjson")
        timeout = aiohttp.ClientTimeout(total=None, sock_read=None)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(yarl.URL(node.chat_url()),
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
    app.router.add_get("/node/pull", node_pull)
    async def list_models(request: web.Request) -> web.Response:
        return web.json_response(
            {"models": sorted({m for n in st.nodes.alive()
                               if n.name not in st.manually_offline
                               for m in n.models})})

    app.router.add_get("/nodes", list_nodes)
    app.router.add_post("/v1/chat/completions", chat_completions)
    app.router.add_get("/v1/models", list_models)

    from jiesuan_gateway.admin import register_admin
    register_admin(app, st)
    return app


async def amain(args: argparse.Namespace) -> None:
    # 端口预检: 给出可操作的错误而不是 traceback (DEPLOYMENT.md §7)
    # 注意: 端口空闲时 probe 会抛 ConnectionRefused — 这是正常路径, 必须吞掉
    try:
        probe = await aiohttp.ClientSession().get(
            f"http://127.0.0.1:{args.port}/nodes", timeout=aiohttp.ClientTimeout(total=2))
        if probe.status == 200:
            existing = await probe.json()
            print(f"[gw] port {args.port} already served by another gateway "
                  f"({existing.get('scheduler')}) — nothing to do. "
                  f"Kill it first: see docs/DEPLOYMENT.md §7", flush=True)
            return
    except (aiohttp.ClientConnectorError, asyncio.TimeoutError, OSError):
        pass  # 端口空闲 → 正常启动
    sched = _get_scheduler(args.scheduler)
    st = GatewayState(scheduler=sched, trace_path=args.trace,
                      node_token=args.node_token, api_key=args.api_key)
    if st.node_token:
        print(f"[gw] 公网模式: 节点鉴权 ON", flush=True)
    if st.api_key:
        print(f"[gw] 公网模式: /v1 Bearer 鉴权 ON", flush=True)
    # 静态哑节点: --static-node name=win,url=http://ip:11434,models=qwen3:4b (可重复)
    for spec in args.static_node or []:
        parts = dict(kv.split("=", 1) for kv in spec.split(",") if "=" in kv)
        parts["models"] = [m for m in parts.get("models", "").split(";") if m]
        st.static_nodes.append(parts)
    st.bootstrap_static()

    # ADR-003 tier 配置 → CapacityManager
    if args.tier:
        kv = {}
        for spec in args.tier:
            k, _, v = spec.partition("=")
            kv[k.lower()] = v
        st.tiers = TierConfig(high=kv.get("high", ""), mid=kv.get("mid", ""), low=kv.get("low", ""))
        st.capacity = CapacityManager(tiers=st.tiers, gateway_port=args.port)
        print(f"[gw] capacity manager ON: tiers high={st.tiers.high} mid={st.tiers.mid} "
              f"low={st.tiers.low}", flush=True)

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
    ap.add_argument("--scheduler", default="L1",
                    help="L0=roundrobin L1=leastpending litellm=L-Prod(latency+failover)")
    ap.add_argument("--trace", default="trace.jsonl")
    ap.add_argument("--static-node", action="append",
                    help="哑节点: name=X,url=http://ip:11434,models=m1;m2 (无agent, 引擎直连)")
    ap.add_argument("--tier", action="append", metavar="HIGH=qwen3:14b",
                    help="档位声明: high/mid/low=模型名 (启用 ADR-003 容量调度)")
    ap.add_argument("--node-token", default=os.environ.get("JIESUAN_NODE_TOKEN", ""),
                    help="公网模式: 节点注册鉴权 token (或环境变量 JIESUAN_NODE_TOKEN)")
    ap.add_argument("--api-key", default=os.environ.get("JIESUAN_API_KEY", ""),
                    help="公网模式: /v1 Bearer 鉴权 (或环境变量 JIESUAN_API_KEY)")
    args = ap.parse_args()
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("[gw] bye", flush=True)


if __name__ == "__main__":
    main()
