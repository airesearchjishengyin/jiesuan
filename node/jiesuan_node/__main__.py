"""jiesuan-node: 借算节点端。

职责:
  1. 启动时向网关注册 (name, engine 能力, 模型清单, 容量/idle 状态)
  2. 周期心跳 (健康 + pending + 容量 + idle) — 网关响应可携带指令
  3. 暴露本地推理引擎 (Ollama/Prima.cpp) 的代理端点给网关回源
  4. 执行网关指令: load_model / unload_model (ADR-003 容量状态机的执行端)

用法:
  python -m jiesuan_node --name mac-air --gateway 192.168.1.5:7800 [--engine http://localhost:11434]
  python -m jiesuan_node --name mac-air --gateway 192.168.1.5:7800 --engine-type prima --prima-role head --prima-model qwen2.5-7b-instruct-q4_k_m.gguf
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import ctypes
import json
import os
import platform
import time
from dataclasses import dataclass, field

import aiohttp
import yarl
from aiohttp import web

from jiesuan_node.mock import MockEngine, MockFailure
from jiesuan_node.prima_engine import PrimaEngine, PrimaConfig, add_prima_args, create_prima_config_from_args


# ---------------------------------------------------------------- data model

@dataclass
class NodeState:
    name: str
    engine_url: str
    gateway_url: str
    os_info: str = field(default_factory=lambda: f"{platform.system()}/{platform.release()}")
    arch: str = field(default_factory=platform.machine)
    models: list[str] = field(default_factory=list)          # 已 pull (磁盘上有)
    loaded_models: list[str] = field(default_factory=list)   # 引擎内存中驻留
    pending: int = 0
    idle: bool = True
    started_at: float = field(default_factory=time.time)
    total_ram_gb: float = 0.0
    used_ram_gb: float = 0.0
    # v0.3: mock (模拟引擎) / pull (出站 WebSocket, 穿 NAT)
    mock: bool = False
    pull: bool = False
    port: int = 7801
    node_token: str = ""     # 公网模式节点鉴权 (wss 网关要求时必填)
    mock_engine: "MockEngine | None" = None
    ws_lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # pull 连接的发送锁


# ---------------------------------------------------------------- system queries

def query_idle() -> bool:
    """用户活动检测: macOS 用 CGEventSource secondsSinceLastEvent (无依赖)。

    Windows 简化版: 返回 True (v0.2 用前台窗口检测增强, 见 roadmap)。
    """
    if platform.system() == "Darwin":
        try:
            cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
            cg.CGEventSourceSecondsSinceLastEvent.restype = ctypes.c_double
            idle_s = cg.CGEventSourceSecondsSinceLastEvent(0)  # kCGHIDEventTap=0
            return idle_s >= 120.0   # 2 分钟无键鼠 = idle
        except Exception:
            return True
    return True   # Windows/Linux: v0.2 增强


def query_memory() -> tuple[float, float]:
    """返回 (total_gb, used_gb)。跨平台最小实现。"""
    system = platform.system()
    try:
        if system == "Darwin":
            import subprocess
            total = int(subprocess.run(["sysctl", "-n", "hw.memsize"],
                                       capture_output=True, text=True).stdout) / 1e9
            vm = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
            import re
            pages = {}
            for line in vm.splitlines():
                m = re.match(r"(.+):\s+(\d+)", line)
                if m:
                    pages[m[1].strip().rstrip(":")] = int(m[2])
            page = 16384
            used = (pages.get("Pages wired down", 0) + pages.get("Pages active", 0)
                    + pages.get("Pages occupied by compressor", 0)) * page / 1e9
            return round(total, 1), round(used, 1)
        if system == "Windows":
            import ctypes.wintypes  # noqa: F401 — 确保 windll 已加载
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):  # type: ignore[name-defined]
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = MEMORYSTATUSEX()  # type: ignore[possibly-undefined]
            m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)  # type: ignore[possibly-undefined]
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))  # type: ignore[attr-defined]
            total = m.ullTotalPhys / 1e9
            used = (m.ullTotalPhys - m.ullAvailPhys) / 1e9
            return round(total, 1), round(used, 1)
        # Linux
        with open("/proc/meminfo") as f:
            info = {l.split(":")[0]: int(l.split()[1]) for l in f if ":" in l}
        total = info["MemTotal"] / 1e6
        used = (info["MemTotal"] - info["MemAvailable"]) / 1e6
        return round(total, 1), round(used, 1)
    except Exception:
        return 0.0, 0.0


async def fetch_models(engine_url: str) -> list[str]:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{engine_url}/api/tags", timeout=aiohttp.ClientTimeout(total=5)) as r:
                data = await r.json()
                return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []


async def fetch_loaded(engine_url: str) -> list[str]:
    """查询引擎内存中驻留的模型 (ollama ps)。"""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{engine_url}/api/ps", timeout=aiohttp.ClientTimeout(total=5)) as r:
                data = await r.json()
                return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []


async def gather_facts(st: NodeState, prima_engine: PrimaEngine | None = None) -> None:
    """心跳前采集引擎事实 — 真实引擎或 mock 替身, 同一 NodeState 字段。"""
    if st.mock and st.mock_engine is not None:
        me = st.mock_engine
        st.total_ram_gb, st.used_ram_gb = await me.ram()
        st.idle = await me.idle()
        st.loaded_models = list(me.loaded)
        st.models = list(me.model_list)
        return
    
    if prima_engine is not None:
        # Prima.cpp engine: 从引擎获取容量信息
        cap = prima_engine.get_capacity_info()
        st.total_ram_gb = cap.get("total_ram_gb", 0.0)
        st.used_ram_gb = cap.get("used_ram_gb", 0.0)
        st.idle = cap.get("idle", True)
        st.models = cap.get("models", [])
        st.loaded_models = cap.get("loaded_models", [])
        return
    
    # Ollama engine (default)
    st.total_ram_gb, st.used_ram_gb = query_memory()
    st.idle = query_idle()
    st.loaded_models = await fetch_loaded(st.engine_url)
    if not st.models:
        st.models = await fetch_models(st.engine_url)


async def exec_command(st: NodeState, op: str, model: str, prima_engine: PrimaEngine | None = None) -> None:
    """执行网关容量指令 — mock、prima.cpp 或真实 Ollama 引擎。"""
    if st.mock and st.mock_engine is not None:
        fn = st.mock_engine.load if op == "load_model" else st.mock_engine.unload
        ok = await fn(model)
    elif prima_engine is not None:
        fn = prima_engine.load_model if op == "load_model" else prima_engine.unload_model
        ok = await fn(model)
    else:
        fn = exec_load if op == "load_model" else exec_unload
        ok = await fn(st.engine_url, model)
    print(f"[node] executing {op} {model} → {'ok' if ok else 'FAILED'}", flush=True)


# ---------------------------------------------------------------- commands

async def exec_load(engine_url: str, model: str, keep_alive: str = "30m") -> bool:
    """加载模型进内存 (ollama 空跑一个 1 token 请求, keep_alive 延长驻留)。"""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{engine_url}/api/generate",
                              json={"model": model, "prompt": "", "keep_alive": keep_alive,
                                    "options": {"num_predict": 1}},
                              timeout=aiohttp.ClientTimeout(total=300)) as r:
                return r.status == 200
    except Exception as e:
        print(f"[node] load_model {model} failed: {e!r}", flush=True)
        return False


async def exec_unload(engine_url: str, model: str) -> bool:
    """卸载: keep_alive=0 立即驱逐。"""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{engine_url}/api/generate",
                              json={"model": model, "keep_alive": 0, "prompt": "",
                                    "options": {"num_predict": 0}},
                              timeout=aiohttp.ClientTimeout(total=30)) as r:
                return r.status == 200
    except Exception as e:
        print(f"[node] unload_model {model} failed: {e!r}", flush=True)
        return False


# ---------------------------------------------------------------- proxy handler

async def proxy_chat(request: web.Request) -> web.StreamResponse:
    st: NodeState = request.app["state"]
    prima_engine: PrimaEngine | None = request.app.get("prima_engine")
    st.pending += 1
    try:
        if st.mock and st.mock_engine is not None:
            body = await request.json()
            gen = st.mock_engine.chat_lines(body)
            try:
                first = await gen.__anext__()   # 失败注入在 prepare 前抛出 → 可干净返回 502
            except StopAsyncIteration:
                return web.json_response({"error": "mock produced no output"}, status=502)
            except MockFailure as e:
                return web.json_response({"error": f"mock failure: {e}"}, status=502)
            resp = web.StreamResponse(status=200,
                                      headers={"Content-Type": "application/x-ndjson"})
            await resp.prepare(request)
            await resp.write((first + "\n").encode())
            async for line in gen:
                await resp.write((line + "\n").encode())
            await resp.write_eof()
            return resp
        
        # Prima.cpp engine
        if prima_engine is not None:
            body = await request.json()
            stream = body.get("stream", True)
            try:
                result = await prima_engine.chat_completions(body, stream=stream)
                if stream:
                    resp = web.StreamResponse(status=200,
                                              headers={"Content-Type": "application/x-ndjson"})
                    await resp.prepare(request)
                    async for chunk in result:
                        await resp.write(chunk)
                    await resp.write_eof()
                    return resp
                else:
                    return web.json_response(result)
            except Exception as e:
                return web.json_response({"error": f"prima engine error: {e}"}, status=502)
        
        # Ollama engine (default)
        body = await request.json()
        url = yarl.URL(f"{st.engine_url}/api/chat")
        timeout = aiohttp.ClientTimeout(total=None, sock_read=None)
        wants_stream = bool(body.get("stream", True))
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(url, json=body,
                              headers={"Content-Type": "application/json"}) as up:
                if not wants_stream:
                    # 非流式: Ollama 返回单 JSON → 转 OpenAI 格式
                    ollama_obj = await up.json(content_type=None)
                    payload = {
                        "id": f"chatcmpl-{int(time.time() * 1000)}",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": ollama_obj.get("model", ""),
                        "choices": [{
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": ollama_obj.get("message", {}).get("content", ""),
                            },
                            "finish_reason": ollama_obj.get("done_reason") or "stop",
                        }],
                        "usage": {
                            "prompt_tokens": ollama_obj.get("prompt_eval_count", 0),
                            "completion_tokens": ollama_obj.get("eval_count", 0),
                            "total_tokens": ollama_obj.get("prompt_eval_count", 0)
                            + ollama_obj.get("eval_count", 0),
                        },
                    }
                    return web.json_response(payload)
                # 流式: Ollama ndjson → 转 OpenAI SSE (Hermes 才能解析)
                resp = web.StreamResponse(
                    status=200,
                    headers={"Content-Type": "text/event-stream",
                             "Cache-Control": "no-cache"})
                await resp.prepare(request)
                cid = f"chatcmpl-{int(time.time() * 1000)}"
                created = int(time.time())
                buf = b""
                finish = "stop"
                async for chunk in up.content.iter_any():
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        content = obj.get("message", {}).get("content", "")
                        if obj.get("done"):
                            finish = obj.get("done_reason") or "stop"
                            continue
                        delta = {"content": content} if content else {}
                        sse = {
                            "id": cid, "object": "chat.completion.chunk",
                            "created": created, "model": obj.get("model", ""),
                            "choices": [{"index": 0, "delta": delta,
                                         "finish_reason": None}],
                        }
                        await resp.write(f"data: {json.dumps(sse, ensure_ascii=False)}\n\n".encode())
                final = {
                    "id": cid, "object": "chat.completion.chunk",
                    "created": created, "model": "ollama",
                    "choices": [{"index": 0, "delta": {},
                                 "finish_reason": finish}],
                }
                await resp.write(f"data: {json.dumps(final, ensure_ascii=False)}\n\n".encode())
                await resp.write(b"data: [DONE]\n\n")
                await resp.write_eof()
                return resp
    except Exception as e:  # noqa: BLE001
        return web.json_response({"error": str(e)}, status=502)
    finally:
        st.pending -= 1


async def proxy_models(request: web.Request) -> web.Response:
    st: NodeState = request.app["state"]
    prima_engine: PrimaEngine | None = request.app.get("prima_engine")
    if prima_engine is not None:
        models = await prima_engine.list_models()
        return web.json_response({"node": st.name, "models": models})
    models = await fetch_models(st.engine_url)
    return web.json_response({"node": st.name, "models": models})


async def health(request: web.Request) -> web.Response:
    st: NodeState = request.app["state"]
    prima_engine: PrimaEngine | None = request.app.get("prima_engine")
    if prima_engine is not None:
        health_info = await prima_engine.health_check()
        return web.json_response({"node": st.name, "ok": health_info.get("ok", False), "pending": st.pending, "engine": "prima", "details": health_info})
    return web.json_response({"node": st.name, "ok": True, "pending": st.pending})


# ---------------------------------------------------------------- main

async def make_app(st: NodeState, prima_engine: PrimaEngine | None = None) -> web.Application:
    app = web.Application()
    app["state"] = st
    app["prima_engine"] = prima_engine
    app.router.add_post("/v1/chat/completions", proxy_chat)
    app.router.add_get("/v1/models", proxy_models)
    app.router.add_get("/health", health)
    return app


# ---------------------------------------------------------------- heartbeat

async def heartbeat_loop(st: NodeState, prima_engine: PrimaEngine | None, interval: float = 5.0) -> None:
    backoff = interval
    first = True
    while True:
        try:
            await gather_facts(st, prima_engine)

            payload = {
                "type": "register" if first else "heartbeat",
                "name": st.name,
                "port": st.port,
                "os": st.os_info,
                "arch": st.arch,
                "models": st.models,
                "loaded_models": st.loaded_models,
                "pending": st.pending,
                "idle": st.idle,
                "total_ram_gb": st.total_ram_gb,
                "used_ram_gb": st.used_ram_gb,
                "uptime": round(time.time() - st.started_at, 1),
                "ts": time.time(),
            }
            async with aiohttp.ClientSession() as s:
                async with s.post(f"{st.gateway_url}/node/{payload['type']}", json=payload,
                                  timeout=aiohttp.ClientTimeout(total=8)) as r:
                    if r.status == 200:
                        first = False
                        backoff = interval
                        cmd = await r.json(content_type=None) or {}
                        for c in cmd.get("commands", []):
                            if c.get("op") in ("load_model", "unload_model"):
                                print(f"[node] got command {c['op']} {c['model']}", flush=True)
                                await exec_command(st, c["op"], c["model"], prima_engine)
                    else:
                        print(f"[node] gateway {r.status}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[node] heartbeat failed: {e!r} retry in {backoff:.0f}s", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.5, 60)
            continue
        await asyncio.sleep(interval)


# ---------------------------------------------------------------- pull mode

PULL_BACKOFF_MAX = 10.0


async def _run_payload(st: NodeState, payload: dict, prima_engine: PrimaEngine | None = None) -> str:
    """本地执行一个推理 payload, 返回完整 ndjson 文本 (pull 隧道专用)。"""
    st.pending += 1
    try:
        lines: list[str] = []
        if st.mock and st.mock_engine is not None:
            async for line in st.mock_engine.chat_lines(payload):
                lines.append(line)
        elif prima_engine is not None:
            # Prima.cpp: 同步调用 chat_completions 并收集完整响应
            result = await prima_engine.chat_completions(payload, stream=False)
            if isinstance(result, dict):
                # 非流式返回 JSON，转为 ndjson 格式
                lines.append(json.dumps(result, ensure_ascii=False))
            else:
                # 不应该到这里，stream=False 时返回 dict
                pass
        else:
            timeout = aiohttp.ClientTimeout(total=None, sock_read=None)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(yarl.URL(f"{st.engine_url}/api/chat"),
                                  json=payload) as up:
                    if up.status != 200:
                        return json.dumps({"error": f"engine status {up.status}"})
                    async for chunk in up.content.iter_any():
                        lines.extend(chunk.decode(errors="replace").splitlines())
        return "\n".join(lines)
    finally:
        st.pending -= 1


async def pull_loop(st: NodeState, prima_engine: PrimaEngine | None = None) -> None:
    """Pull 模式: 出站 WebSocket 挂到网关, 网关把请求沿连接派下来。

    出站连接天然穿 NAT — 办公网/家庭网/公网云机器统一走此模式。
    gateway_url 支持 http:// (ws://) 与 wss:// (公网经 Cloudflare Tunnel, 必须加密)。
    --node-token: 公网模式节点鉴权, 随 hello 下发, 网关校验失败即拒。
    心跳与容量指令都走同一条 WS; 断线指数退避重连。
    """
    if st.gateway_url.startswith("wss://") or st.gateway_url.startswith("https://"):
        ws_url = st.gateway_url.split("//", 1)[0] + "//" + \
            st.gateway_url.split("//", 1)[1].rstrip("/") + "/node/pull"
    else:
        host = st.gateway_url.split("//")[-1]
        ws_url = f"ws://{host}/node/pull"
    backoff = 1.0
    while True:
        try:
            session = aiohttp.ClientSession()
            try:
                async with session.ws_connect(ws_url, heartbeat=15) as ws:
                    print(f"[node] pull 连接已建立 → {ws_url}", flush=True)
                    backoff = 1.0
                    await gather_facts(st, prima_engine)
                    hello = {
                        "type": "register", "transport": "ws", "port": st.port,
                        "token": st.node_token,
                        "name": st.name, "os": st.os_info, "arch": st.arch,
                        "models": st.models, "loaded_models": st.loaded_models,
                        "pending": st.pending, "idle": st.idle,
                        "total_ram_gb": st.total_ram_gb, "used_ram_gb": st.used_ram_gb,
                        "uptime": round(time.time() - st.started_at, 1), "ts": time.time(),
                    }
                    await ws.send_json(hello)

                    async def hb_sender() -> None:
                        """WS 上的周期心跳 — 网关响应可携带容量指令。"""
                        while True:
                            await asyncio.sleep(5.0)
                            await gather_facts(st, prima_engine)
                            await ws.send_json({
                                "type": "heartbeat", "name": st.name,
                                "models": st.models, "loaded_models": st.loaded_models,
                                "pending": st.pending, "idle": st.idle,
                                "total_ram_gb": st.total_ram_gb, "used_ram_gb": st.used_ram_gb,
                                "uptime": round(time.time() - st.started_at, 1), "ts": time.time(),
                            })

                    hb_task = asyncio.create_task(hb_sender())
                    try:
                        # 注意: 服务端 close(4001) 时 aiohttp 客户端的 async-for
                        # 不 yield 任何消息直接结束, 拒绝检测必须读 ws.close_code
                        async for msg in ws:
                            if msg.type != aiohttp.WSMsgType.TEXT:
                                break
                            data = json.loads(msg.data)
                            op = data.get("op")
                            if op == "chat":
                                async with st.ws_lock:
                                    try:
                                        result = await _run_payload(st, data.get("body", {}), prima_engine)
                                    except Exception as e:  # noqa: BLE001
                                        result = json.dumps({"error": str(e)[:200]})
                                    await ws.send_json({"type": "result", "op": "result",
                                                        "rid": data.get("rid"),
                                                        "node": st.name, "payload": result})
                            elif op == "command":
                                c = data.get("command", {})
                                if c.get("op") in ("load_model", "unload_model"):
                                    print(f"[node] got command {c['op']} {c['model']}", flush=True)
                                    await exec_command(st, c["op"], c["model"], prima_engine)
                    finally:
                        hb_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await hb_task
            finally:
                await session.close()
            if ws.close_code == 4001:
                print("[node] ✗ 网关拒绝注册 (token 无效) — 检查 JIESUAN_NODE_TOKEN 后重启", flush=True)
                raise SystemExit(3)
        except Exception as e:  # noqa: BLE001
            print(f"[node] pull 断开: {e!r} → {backoff:.0f}s 后重连", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, PULL_BACKOFF_MAX)


# ---------------------------------------------------------------- entry point

async def amain(args: argparse.Namespace) -> None:
    # gateway 支持 host:port (ws) 或 wss://domain (公网经 CF Tunnel, 自动升级加密)
    gw = args.gateway
    if gw.startswith("wss://") or gw.startswith("https://"):
        gw_url = gw if gw.startswith("wss://") else gw.replace("https://", "wss://", 1)
    else:
        gw_url = f"http://{gw}"
    st = NodeState(
        name=args.name,
        engine_url=args.engine.rstrip("/"),
        gateway_url=gw_url,
        mock=args.mock,
        pull=args.pull,
        port=args.port,
        node_token=os.environ.get("JIESUAN_NODE_TOKEN", ""),
    )
    
    prima_engine: PrimaEngine | None = None
    
    if args.mock:
        spec = {}
        if args.mock_spec:
            for kv in args.mock_spec.split(","):
                k, _, v = kv.partition("=")
                spec[k.strip()] = v.strip()
        from jiesuan_node.mock import MockEngine
        st.mock_engine = MockEngine(
            name=args.name,
            model_list=[m for m in spec.get("models", "mock-model").split(";") if m],
            ram_gb=float(spec.get("ram_gb", 16)),
            delay=float(spec.get("delay", 0.2)),
            jitter=float(spec.get("jitter", 0.1)),
            fail_p=float(spec.get("fail_p", 0.0)),
            busy=spec.get("busy", "") == "1",
        )
    elif args.engine_type == "prima":
        # Prima.cpp engine
        prima_config = create_prima_config_from_args(args)
        prima_engine = PrimaEngine(prima_config)
        # 启动 prima.cpp 进程
        ok = await prima_engine.start()
        if not ok:
            print(f"[node] Failed to start prima.cpp engine", flush=True)
            return
        # 预加载模型列表
        st.models = await prima_engine.list_models()
        if prima_config.model_file:
            st.loaded_models = [prima_config.model_file]
    else:
        # Ollama engine (default)
        st.models = await fetch_models(st.engine_url)
    
    app = await make_app(st, prima_engine)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", args.port)
    await site.start()
    
    tasks: list[asyncio.Task] = []
    if st.pull:
        tasks.append(asyncio.create_task(pull_loop(st, prima_engine)))
    else:
        tasks.append(asyncio.create_task(heartbeat_loop(st, prima_engine)))
    
    engine_desc = "mock" if st.mock else ("prima" if args.engine_type == "prima" else "ollama")
    mode = ("pull" if st.pull else "push")
    print(f"[node] '{st.name}' up on :{args.port} → gateway {st.gateway_url} "
          f"(engine={engine_desc}, mode={mode}, models: {', '.join(st.models) or 'none'})", flush=True)
    
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.gather(*tasks)
        await asyncio.Event().wait()


def main() -> None:
    ap = argparse.ArgumentParser("jiesuan-node")
    ap.add_argument("--name", required=True)
    ap.add_argument("--gateway", required=True,
                    help="网关地址 host:port 或 wss://域名 (公网经 CF Tunnel)")
    ap.add_argument("--engine", default="http://localhost:11434")
    ap.add_argument("--port", type=int, default=7801)
    ap.add_argument("--pull", action="store_true",
                    help="pull 模式: 出站 WebSocket 挂网关 (穿 NAT, 无需端口转发)")
    ap.add_argument("--mock", action="store_true",
                    help="mock 模式: 用 MockEngine 替代 Ollama (逻辑外推/故障注入测试)")
    ap.add_argument("--mock-spec",
                    help="mock 参数: models=m1;m2,ram_gb=16,delay=0.2,jitter=0.1,fail_p=0.1,busy=1")
    
    # Prima.cpp engine arguments
    add_prima_args(ap)
    
    args = ap.parse_args()
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("[node] bye", flush=True)


if __name__ == "__main__":
    main()