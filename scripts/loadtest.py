"""loadtest.py — jiesuan 网关并发负载测试。

用法: .venv/bin/python scripts/loadtest.py [并发数=50] [总请求=300] [模型=qwen3:4b]
网关: 环境变量 JIESUAN_GATEWAY (默认 http://127.0.0.1:7800)
输出: 摘要 JSON (RPS/成功率/延迟分位) + 明细 /tmp/loadtest_<model>.json
"""
import asyncio
import json
import os
import sys
import time

import aiohttp

GW = os.environ.get("JIESUAN_GATEWAY", "http://127.0.0.1:7800")


async def worker(session: aiohttp.ClientSession, sem: asyncio.Semaphore,
                 results: list) -> None:
    async with sem:
        t0 = time.time()
        try:
            async with session.post(
                    f"{GW}/v1/chat/completions",
                    json={"model": MODEL,
                          "messages": [{"role": "user", "content": "hi"}],
                          "max_tokens": 5},
                    timeout=aiohttp.ClientTimeout(total=30)) as r:
                body = await r.read()
                dt = time.time() - t0
                ok = r.status == 200 and b'"done": true' in body
                results.append({"ok": ok, "status": r.status, "lat": round(dt, 3)})
        except Exception as e:  # noqa: BLE001
            results.append({"ok": False, "status": 0,
                            "lat": round(time.time() - t0, 3), "err": str(e)[:60]})


async def main() -> None:
    sem = asyncio.Semaphore(CONC)
    results: list = []
    t_start = time.time()
    async with aiohttp.ClientSession() as s:
        await asyncio.gather(*[worker(s, sem, results) for _ in range(TOTAL)])
    wall = time.time() - t_start
    oks = [r for r in results if r["ok"]]
    lats = sorted(r["lat"] for r in oks)

    def pct(p: float):
        return round(lats[min(int(len(lats) * p), len(lats) - 1)], 3) if lats else None

    summary = {
        "gateway": GW, "model": MODEL, "total": TOTAL, "concurrency": CONC,
        "wall_s": round(wall, 1), "rps": round(TOTAL / wall, 1),
        "success": len(oks), "fail": TOTAL - len(oks),
        "success_rate": round(len(oks) / TOTAL, 3),
        "client_p50": pct(0.5), "client_p90": pct(0.9),
        "client_p99": pct(0.99), "client_max": lats[-1] if lats else None,
    }
    print(json.dumps(summary, ensure_ascii=False))
    with open(f"/tmp/loadtest_{MODEL.replace(':', '_')}.json", "w") as f:
        json.dump(results, f)


CONC = int(sys.argv[1]) if len(sys.argv) > 1 else 50
TOTAL = int(sys.argv[2]) if len(sys.argv) > 2 else 300
MODEL = sys.argv[3] if len(sys.argv) > 3 else "qwen3:4b"
asyncio.run(main())
