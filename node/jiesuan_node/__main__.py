"""jiesuan-node: 借算节点端。

职责:
  1. 启动时向网关注册 (name, engine 能力, 模型清单, 容量/idle 状态)
  2. 周期心跳 (健康 + pending + 容量 + idle) — 网关响应可携带指令
  3. 暴露本地推理引擎 (Ollama) 的代理端点给网关回源
  4. 执行网关指令: load_model / unload_model (ADR-003 容量状态机的执行端)

用法:
  python -m jiesuan_node --name mac-air --gateway 192.168.1.5:7800 [--engine http://localhost:11434]
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import ctypes
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
    models: list[str] = field(default_factory=list)          # 已 pull (磁盘上有)
    loaded_models: list[str] = field(default_factory=list)   # 引擎内存中驻留
    pending: int = 0
    idle: bool = True
    started_at: float = field(default_factory=time.time)
    total_ram_gb: float = 0.0
    used_ram_gb: float = 0.0


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
    st.pending += 1
    try:
        body = await request.read()
        url = yarl.URL(f"{st.engine_url}/api/chat")
        timeout = aiohttp.ClientTimeout(total=None, sock_read=None)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(url, data=body,
                              headers={"Content-Type": "application/json"}) as up:
                resp = web.StreamResponse(
                    status=up.status,
                    headers={"Content-Type": up.headers.get("Content-Type", "application/json")})
                await resp.prepare(request)
                async for chunk in up.content.iter_any():
                    await resp.write(chunk)
                await resp.write_eof()
                return resp
    except Exception as e:  # noqa: BLE001
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
    backoff = interval
    first = True
    while True:
        try:
            st.total_ram_gb, st.used_ram_gb = query_memory()
            st.idle = query_idle()
            st.loaded_models = await fetch_loaded(st.engine_url)
            if not st.models:
                st.models = await fetch_models(st.engine_url)

            payload = {
                "type": "register" if first else "heartbeat",
                "name": st.name,
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
                            if c.get("op") == "load_model":
                                print(f"[node] executing load_model {c['model']}", flush=True)
                                await exec_load(st.engine_url, c["model"])
                            elif c.get("op") == "unload_model":
                                print(f"[node] executing unload_model {c['model']} (user active)", flush=True)
                                await exec_unload(st.engine_url, c["model"])
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
    ap.add_argument("--name", required=True)
    ap.add_argument("--gateway", required=True)
    ap.add_argument("--engine", default="http://localhost:11434")
    ap.add_argument("--port", type=int, default=7801)
    args = ap.parse_args()
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("[node] bye", flush=True)


if __name__ == "__main__":
    main()
