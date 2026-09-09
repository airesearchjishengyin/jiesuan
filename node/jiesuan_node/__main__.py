"""jiesuan-node: 借算节点端。

职责:
  1. 启动时向网关注册 (name, engine 能力, 模型清单)
  2. 周期心跳 (健康度 + pending 请求数)
  3. 暴露本地推理引擎 (Ollama) 的代理端点给网关回源

用法:
  python -m jiesuan_node --name mac-air --gateway 192.168.1.5:7800 [--engine http://localhost:11434]
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import platform
import time
from dataclasses import dataclass, field

import aiohttp
import yarl
from aiohttp import web


# ---------------------------------------------------------------- data model

@dataclass
class NodeState:
    name: str
    engine_url: str
    gateway_url: str
    os_info: str = field(default_factory=lambda: f"{platform.system()}/{platform.release()}")
    arch: str = field(default_factory=platform.machine)
    models: list[str] = field(default_factory=list)
    pending: int = 0          # 正在处理的请求数 (L1 调度信号)
    started_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------- ollama helpers

async def fetch_models(engine_url: str) -> list[str]:
    """从本地引擎拉取已安装模型清单。"""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{engine_url}/api/tags", timeout=aiohttp.ClientTimeout(total=5)) as r:
                data = await r.json()
                return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []


# ---------------------------------------------------------------- proxy handler

async def proxy_chat(request: web.Request) -> web.StreamResponse:
    """网关回源: 转发 /v1/chat/completions 到本地引擎, 流式透传。

    pending 计数是 L1 负载感知的核心信号 — 进入+1, 结束-1。
    """
    st: NodeState = request.app["state"]
    st.pending += 1
    try:
        body = await request.read()
        url = yarl.URL(f"{st.engine_url}/api/chat")
        timeout = aiohttp.ClientTimeout(total=None, sock_read=None)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(url, data=body,
                              headers={"Content-Type": "application/json"}) as up:
                # 流式透传 (SSE/NDJSON 原样转发, 网关不改内容)
                resp = web.StreamResponse(
                    status=up.status,
                    headers={"Content-Type": up.headers.get("Content-Type", "application/json")})
                await resp.prepare(request)
                async for chunk in up.content.iter_any():
                    await resp.write(chunk)
                await resp.write_eof()
                return resp
    except Exception as e:  # noqa: BLE001 — 节点侧任何异常都不应崩溃心跳循环
        return web.json_response({"error": str(e)}, status=502)
    finally:
        st.pending -= 1


async def proxy_models(request: web.Request) -> web.Response:
    st: NodeState = request.app["state"]
    models = await fetch_models(st.engine_url)
    return web.json_response({"node": st.name, "models": models})


async def health(request: web.Request) -> web.Response:
    st: NodeState = request.app["state"]
    return web.json_response({"node": st.name, "ok": True, "pending": st.pending})


# ---------------------------------------------------------------- heartbeat

async def heartbeat_loop(st: NodeState, interval: float = 5.0) -> None:
    """向网关注册 + 周期心跳。失败不退出, 指数退避重试 (最多 60s)。"""
    backoff = interval
    first = True
    while True:
        try:
            if not st.models:
                st.models = await fetch_models(st.engine_url)
            payload = {
                "type": "register" if first else "heartbeat",
                "name": st.name,
                "os": st.os_info,
                "arch": st.arch,
                "models": st.models,
                "pending": st.pending,
                "uptime": round(time.time() - st.started_at, 1),
                "ts": time.time(),
            }
            async with aiohttp.ClientSession() as s:
                async with s.post(f"{st.gateway_url}/node/{payload['type']}", json=payload,
                                  timeout=aiohttp.ClientTimeout(total=5)) as r:
                    if r.status == 200:
                        first = False
                        backoff = interval
                        # 网关可以下发控制指令 (如: 重拉模型清单)
                        cmd = await r.json(content_type=None) or {}
                        if cmd.get("refresh_models"):
                            st.models = await fetch_models(st.engine_url)
                    else:
                        print(f"[node] gateway {r.status}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[node] heartbeat failed: {e!r} retry in {backoff:.0f}s", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.5, 60)
            continue
        await asyncio.sleep(interval)


# ---------------------------------------------------------------- main

async def make_app(st: NodeState) -> web.Application:
    app = web.Application()
    app["state"] = st
    app.router.add_post("/v1/chat/completions", proxy_chat)
    app.router.add_get("/v1/models", proxy_models)
    app.router.add_get("/health", health)
    return app


async def amain(args: argparse.Namespace) -> None:
    st = NodeState(
        name=args.name,
        engine_url=args.engine.rstrip("/"),
        gateway_url=f"http://{args.gateway}",
    )
    st.models = await fetch_models(st.engine_url)
    app = await make_app(st)
    hb = asyncio.create_task(heartbeat_loop(st))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", args.port)
    await site.start()
    print(f"[node] '{st.name}' up on :{args.port} → gateway {st.gateway_url} "
          f"(models: {', '.join(st.models) or 'none'})", flush=True)
    with contextlib.suppress(asyncio.CancelledError):
        await hb
        await asyncio.Event().wait()


def main() -> None:
    ap = argparse.ArgumentParser("jiesuan-node")
    ap.add_argument("--name", required=True, help="节点名 (集群内唯一)")
    ap.add_argument("--gateway", required=True, help="网关地址 host:port")
    ap.add_argument("--engine", default="http://localhost:11434", help="本地推理引擎 (Ollama)")
    ap.add_argument("--port", type=int, default=7801, help="本节点代理端口")
    args = ap.parse_args()
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("[node] bye", flush=True)


if __name__ == "__main__":
    main()
