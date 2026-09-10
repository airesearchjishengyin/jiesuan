"""Admin API + Node on/off 控制台 (ADR-003 配套)。

- GET  /admin/nodes                     → 节点详细状态 (含 capacity/idle/驻留)
- POST /admin/node/{name}/online        → 手动上线 (清除手动摘流)
- POST /admin/node/{name}/offline       → 手动下线 (摘流, 不杀引擎)
- GET  /dashboard                       → 可视化控制台 (单文件 HTML)
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from aiohttp import web

from jiesuan_gateway.__main__ import GatewayState, HEARTBEAT_TIMEOUT

DASHBOARD_HTML_PATH = Path(__file__).parent / "dashboard.html"


def _node_snapshot(st: GatewayState, now: float) -> list[dict]:
    out = []
    for n in st.nodes.all():
        alive = (now - n.last_seen) <= HEARTBEAT_TIMEOUT
        cap = st.capacity.nodes.get(n.name) if st.capacity else None
        out.append({
            "name": n.name,
            "os": n.os,
            "arch": n.arch,
            "kind": n.kind,
            "alive": alive,
            "online": alive and n.name not in st.manually_offline,
            "manual_offline": n.name in st.manually_offline,
            "models_disk": n.models,
            "loaded": cap.loaded_models if cap else (n.models if alive else []),
            "pending": n.pending,
            "idle": cap.idle if cap else None,
            "total_ram_gb": cap.total_ram_gb if cap else None,
            "used_ram_gb": cap.used_ram_gb if cap else None,
            "capacity_gb": round(cap.capacity_gb(), 1) if cap else None,
            "proxy_url": n.proxy_url,
        })
    return out


def register_admin(app: web.Application, st: GatewayState) -> None:
    async def admin_nodes(request: web.Request) -> web.Response:
        return web.json_response({
            "scheduler": st.scheduler.name,
            "ts": time.time(),
            "nodes": _node_snapshot(st, time.time()),
        })

    async def node_online(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        st.manually_offline.discard(name)
        print(f"[admin] {name} → ONLINE (manual)", flush=True)
        return web.json_response({"ok": True, "name": name, "online": True})

    async def node_offline(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        st.manually_offline.add(name)
        print(f"[admin] {name} → OFFLINE (manual, drained)", flush=True)
        return web.json_response({"ok": True, "name": name, "online": False})

    async def dashboard(request: web.Request) -> web.Response:
        return web.Response(text=DASHBOARD_HTML_PATH.read_text(),
                            content_type="text/html", charset="utf-8")

    app.router.add_get("/admin/nodes", admin_nodes)
    app.router.add_post("/admin/node/{name}/online", node_online)
    app.router.add_post("/admin/node/{name}/offline", node_offline)
    app.router.add_get("/dashboard", dashboard)
