"""MockEngine — 无 Ollama 的模拟推理引擎 (jiesuan 逻辑外推测试)。

目的: 在两台开发机上模拟 "N 台异构节点" 的行为, 验证网关的:
  - 能力异构路由 (不同节点注册不同模型表, 大模型不发给小机器)
  - 部分故障注入 (fail_p → 网关应感知并把失败暴露给调用方)
  - 负载/延迟差异 (delay/jitter → L1 调度器的竞争压力)
  - 容量指令回路 (load_model/unload_model 的执行与状态上报)

保真度: 输出与真实链路完全一致的 Ollama /api/chat ndjson 流,
网关与客户端零改动; 内存/idle/驻留走同一心跳字段。
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import datetime, timezone


class MockFailure(Exception):
    """注入式故障: 模拟节点请求失败 (引擎崩溃/OOM/网络抖动)。"""


class MockEngine:
    def __init__(self, name: str, model_list: list[str], ram_gb: float,
                 delay: float = 0.2, jitter: float = 0.1,
                 fail_p: float = 0.0, busy: bool = False) -> None:
        self.name = name
        self.model_list = model_list
        self.ram_gb = ram_gb
        self.delay = delay
        self.jitter = jitter
        self.fail_p = fail_p
        self.busy = busy          # True = 永远上报"用户正在用" (容量回收测试)
        self.loaded: list[str] = []
        self.reqs = 0

    # ---- 心跳字段 (属性直读, 与 fetch_models 同形由节点端包装) ------------

    async def ram(self) -> tuple[float, float]:
        used = round(self.ram_gb * random.uniform(0.35, 0.55), 1)
        return self.ram_gb, used

    async def idle(self) -> bool:
        return not self.busy

    # ---- 容量指令执行 ----------------------------------------------------

    async def load(self, model: str, keep_alive: str = "30m") -> bool:
        await asyncio.sleep(0.3)                      # 模拟加载耗时
        if model not in self.loaded:
            self.loaded.append(model)
        return True

    async def unload(self, model: str) -> bool:
        self.loaded = [m for m in self.loaded if m != model]
        return True

    # ---- 推理 -----------------------------------------------------------

    async def chat_lines(self, body: dict):
        """产出与 Ollama /api/chat 相同格式的 ndjson 行 (真实链路零差异)。"""
        if self.fail_p > 0 and random.random() < self.fail_p:
            raise MockFailure(f"injected failure (p={self.fail_p})")
        self.reqs += 1
        model = body.get("model", "mock")
        words = [f"[{self.name}]", "mock", "reply", f"#{self.reqs}"]
        n = len(words)
        t0 = time.time()
        for w in words:
            await asyncio.sleep(max(0.0, self.delay / n
                                    + random.uniform(-self.jitter, self.jitter) / n))
            yield json.dumps({"model": model,
                              "created_at": datetime.now(timezone.utc).isoformat(),
                              "message": {"role": "assistant", "content": w},
                              "done": False}, ensure_ascii=False)
        yield json.dumps({"model": model,
                          "created_at": datetime.now(timezone.utc).isoformat(),
                          "message": {"role": "assistant", "content": ""},
                          "done": True,
                          "total_duration": int((time.time() - t0) * 1e9),
                          "eval_count": n,
                          "eval_duration": int((time.time() - t0) * 1e9)},
                         ensure_ascii=False)
