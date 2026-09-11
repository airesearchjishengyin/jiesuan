"""analyze_trace.py — 压测 trace 分析 (路由分布 / 网关延迟分位 / 错误)。

用法: .venv/bin/python scripts/analyze_trace.py [trace路径=trace.jsonl]
"""
import collections
import json
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "trace.jsonl"
routes: collections.Counter = collections.Counter()
done_lat: list = []
errs = 0

for line in open(path):
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        continue
    if d.get("event") == "route":
        routes[d["node"]] += 1
    elif d.get("event") == "done":
        done_lat.append(d["elapsed"])
    elif d.get("event") == "error":
        errs += 1

print(f"trace: {path}")
print(f"== 路由分布 == 总请求 {sum(routes.values())}, 被调度节点 {len(routes)}")
print(f"  top5: {routes.most_common(5)}")
print(f"  bottom5: {routes.most_common()[-5:]}")
if routes:
    print(f"  最大/最小负载比: {max(routes.values())}/{min(routes.values())}")
if done_lat:
    done_lat.sort()
    n = len(done_lat)
    print(f"== 网关侧延迟 == done={n} err={errs} "
          f"p50={done_lat[n // 2]:.3f}s p90={done_lat[int(n * 0.9)]:.3f}s "
          f"p99={done_lat[int(n * 0.99)]:.3f}s max={done_lat[-1]:.3f}s")
else:
    print(f"== 网关侧延迟 == 无 done 事件, err={errs}")
