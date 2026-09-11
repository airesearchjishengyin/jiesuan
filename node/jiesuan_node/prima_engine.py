"""PrimaEngine — prima.cpp (llama.cpp 分布式版) 推理引擎适配器。

职责：
1. 管理 prima.cpp 进程 (llama-server 作为 head, llama-cli 作为 worker)
2. 提供与 Ollama 兼容的 /v1/chat/completions 代理接口
3. 实现模型列表、加载/卸载、健康检查、心跳上报字段
4. 支持单机模式 (prima.cpp 退化为 llama.cpp) 和分布式模式 (head+workers)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp


@dataclass
class PrimaConfig:
    """prima.cpp 运行配置"""
    # 基础路径
    prima_root: str = "/Users/xbowlove/prima.cpp"          # prima.cpp 编译目录
    model_dir: str = "download"                            # 模型目录 (相对 prima_root)
    
    # 角色与拓扑
    role: str = "head"                                     # head | worker
    world_size: int = 1                                    # 总节点数
    rank: int = 0                                          # 当前节点 rank (0=head)
    master_ip: str = "127.0.0.1"                           # head 节点 IP
    next_ip: str = "127.0.0.1"                             # 下一个节点 IP (环状)
    
    # 网络端口
    http_port: int = 8080                                  # llama-server HTTP 端口 (仅 head)
    data_port: int = 9000                                  # 数据平面端口 (环状通信)
    signal_port: int = 10000                               # 信令端口 (环状通信)
    
    # GPU/CPU 配置
    gpu_mem_gb: int = 0                                    # 限制 VRAM 使用 (GB), 0=不限制
    n_gpu_layers: int = -1                                 # GPU 层数 (-1=全量, 0=纯CPU)
    keep_out_in_cuda: bool = False                         # 输出层放 GPU
    
    # 性能优化
    prefetch: bool = True                                  # 启用预取
    force_prefetch: bool = False                           # 强制预取
    ctx_size: int = 4096                                   # 上下文长度
    batch_size: int = 512                                  # 批处理大小
    cont_batching: bool = True                             # 连续批处理 (server 模式)
    
    # 模型
    model_file: str = ""                                   # 具体模型文件名 (如 qwen2.5-7b-instruct-q4_k_m.gguf)
    
    def __post_init__(self):
        self.prima_root = os.path.expanduser(self.prima_root)
        self.model_path = os.path.join(self.prima_root, self.model_dir, self.model_file)
        self.llama_server = os.path.join(self.prima_root, "llama-server")
        self.llama_cli = os.path.join(self.prima_root, "llama-cli")


class PrimaProcessManager:
    """管理 prima.cpp 子进程的生命周期"""
    
    def __init__(self, config: PrimaConfig):
        self.config = config
        self.process: asyncio.subprocess.Process | None = None
        self._started_at: float = 0
        self._model_loaded: str = ""
        
    def _build_cmd(self) -> list[str]:
        """构建启动命令"""
        if self.config.role == "head":
            cmd = [self.config.llama_server]
        else:
            cmd = [self.config.llama_cli]
            
        # 模型文件
        cmd += ["-m", self.config.model_path]
        
        # 分布式参数
        if self.config.world_size > 1:
            cmd += [
                "--world", str(self.config.world_size),
                "--rank", str(self.config.rank),
                "--master", self.config.master_ip,
                "--next", self.config.next_ip,
            ]
            if self.config.prefetch:
                cmd += ["--prefetch"]
            if self.config.force_prefetch:
                cmd += ["--force"]
        
        # GPU/CPU 配置
        if self.config.gpu_mem_gb > 0:
            cmd += ["--gpu-mem", str(self.config.gpu_mem_gb)]
        if self.config.n_gpu_layers >= 0:
            cmd += ["-ngl", str(self.config.n_gpu_layers)]
        if self.config.keep_out_in_cuda:
            cmd += ["--keep-out-in-cuda"]
            
        # 上下文与批处理
        cmd += ["-c", str(self.config.ctx_size)]
        if self.config.role == "head" and self.config.cont_batching:
            cmd += ["-np", str(self.config.batch_size), "--cont-batching"]
            
        # 网络端口
        if self.config.role == "head":
            cmd += ["--host", "0.0.0.0", "--port", str(self.config.http_port)]
        if self.config.world_size > 1:
            cmd += ["--data-port", str(self.config.data_port)]
            cmd += ["--signal-port", str(self.config.signal_port)]
            
        # 单机模式默认参数
        if self.config.world_size == 1 and self.config.role == "head":
            # 单机当作普通 llama.cpp 用
            pass
            
        return cmd
    
    async def start(self) -> bool:
        """启动 prima.cpp 进程"""
        if self.process is not None:
            return True
            
        cmd = self._build_cmd()
        print(f"[PrimaEngine] Starting: {' '.join(shlex.quote(c) for c in cmd)}", flush=True)
        
        # 验证二进制存在
        for bin_path in [self.config.llama_server, self.config.llama_cli]:
            if not os.path.exists(bin_path):
                print(f"[PrimaEngine] Binary not found: {bin_path}", flush=True)
                return False
                
        if not os.path.exists(self.config.model_path):
            print(f"[PrimaEngine] Model not found: {self.config.model_path}", flush=True)
            return False
            
        try:
            # 设置工作目录为 prima_root，这样相对路径的模型也能找到
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self.config.prima_root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._started_at = time.time()
            
            # 等待启动就绪 (head 模式检查 HTTP 端口，worker 模式给几秒缓冲)
            if self.config.role == "head":
                await self._wait_ready()
            else:
                await asyncio.sleep(3)  # worker 启动缓冲
                
            print(f"[PrimaEngine] Started (pid={self.process.pid})", flush=True)
            return True
        except Exception as e:
            print(f"[PrimaEngine] Start failed: {e}", flush=True)
            return False
    
    async def _wait_ready(self, timeout: float = 60.0) -> bool:
        """等待 HTTP 服务就绪"""
        url = f"http://127.0.0.1:{self.config.http_port}/health"
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(url, timeout=aiohttp.ClientTimeout(total=2)) as r:
                        if r.status == 200:
                            return True
            except Exception:
                pass
            await asyncio.sleep(0.5)
        return False
    
    async def stop(self) -> None:
        """停止进程"""
        if self.process is not None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
            self.process = None
            print(f"[PrimaEngine] Stopped", flush=True)
    
    async def health_check(self) -> dict[str, Any]:
        """健康检查"""
        if self.process is None or self.process.returncode is not None:
            return {"ok": False, "reason": "process not running"}
            
        if self.config.role == "head":
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(
                        f"http://127.0.0.1:{self.config.http_port}/health",
                        timeout=aiohttp.ClientTimeout(total=3)
                    ) as r:
                        if r.status == 200:
                            data = await r.json()
                            return {"ok": True, "details": data}
            except Exception as e:
                return {"ok": False, "reason": f"health check failed: {e}"}
        else:
            # worker: 只要进程活着就算健康
            return {"ok": True, "details": {"role": "worker", "pid": self.process.pid}}
            
        return {"ok": False, "reason": "unknown"}
    
    def get_loaded_model(self) -> str:
        return self._model_loaded
    
    def set_loaded_model(self, model: str) -> None:
        self._model_loaded = model


class PrimaEngine:
    """prima.cpp 引擎适配器，提供与 Ollama 兼容的接口"""
    
    def __init__(self, config: PrimaConfig):
        self.config = config
        self.manager = PrimaProcessManager(config)
        self._models_cache: list[str] = []
        _models_cache_time: float = 0
        
    async def start(self) -> bool:
        """启动引擎"""
        return await self.manager.start()
    
    async def stop(self) -> None:
        await self.manager.stop()
    
    async def list_models(self) -> list[str]:
        """获取可用模型列表 (扫描 model_dir)"""
        model_dir = Path(self.config.prima_root) / self.config.model_dir
        if not model_dir.exists():
            return []
        models = []
        for f in model_dir.glob("*.gguf"):
            models.append(f.name)
        self._models_cache = models
        return models
    
    async def list_loaded_models(self) -> list[str]:
        """获取已加载模型"""
        loaded = self.manager.get_loaded_model()
        return [loaded] if loaded else []
    
    async def load_model(self, model: str, keep_alive: str = "30m") -> bool:
        """加载模型 (prima.cpp 单模型常驻，这里记录状态)"""
        # prima.cpp 启动时就加载了模型，这里只是更新状态
        # 如果要切换模型，需要重启进程
        if model != self.manager.get_loaded_model():
            # 模型切换需要重启
            model_path = Path(self.config.prima_root) / self.config.model_dir / model
            if not model_path.exists():
                print(f"[PrimaEngine] Model file not found: {model_path}", flush=True)
                return False
            self.config.model_file = model
            self.config.model_path = str(model_path)
            await self.manager.stop()
            ok = await self.manager.start()
            if ok:
                self.manager.set_loaded_model(model)
            return ok
        return True
    
    async def unload_model(self, model: str) -> bool:
        """卸载模型 (停止进程)"""
        if model == self.manager.get_loaded_model():
            await self.manager.stop()
            self.manager.set_loaded_model("")
            return True
        return False
    
    async def chat_completions(self, body: dict, stream: bool = True) -> Any:
        """代理到 llama-server /v1/chat/completions"""
        if self.config.role != "head":
            raise RuntimeError("chat_completions only supported on head node")
            
        url = f"http://127.0.0.1:{self.config.http_port}/v1/chat/completions"
        timeout = aiohttp.ClientTimeout(total=None, sock_read=None)
        
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(url, json=body) as resp:
                if stream:
                    # 返回流式响应生成器
                    async def gen():
                        async for chunk in resp.content.iter_any():
                            yield chunk
                    return gen()
                else:
                    return await resp.json()
    
    async def health_check(self) -> dict[str, Any]:
        return await self.manager.health_check()
    
    def get_capacity_info(self) -> dict[str, Any]:
        """获取容量信息，用于心跳上报"""
        import psutil
        mem = psutil.virtual_memory()
        return {
            "total_ram_gb": round(mem.total / 1e9, 1),
            "used_ram_gb": round((mem.total - mem.available) / 1e9, 1),
            "idle": True,  # prima.cpp 不抢占用户前台，简化处理
            "models": self._models_cache,
            "loaded_models": [self.manager.get_loaded_model()] if self.manager.get_loaded_model() else [],
        }


def create_prima_config_from_args(args) -> PrimaConfig:
    """从 CLI 参数创建 PrimaConfig"""
    return PrimaConfig(
        prima_root=args.prima_root,
        model_dir=args.prima_model_dir,
        role=args.prima_role,
        world_size=args.prima_world,
        rank=args.prima_rank,
        master_ip=args.prima_master,
        next_ip=args.prima_next,
        http_port=args.prima_http_port,
        data_port=args.prima_data_port,
        signal_port=args.prima_signal_port,
        gpu_mem_gb=args.prima_gpu_mem,
        n_gpu_layers=args.prima_ngl,
        keep_out_in_cuda=args.prima_keep_out_in_cuda,
        prefetch=args.prima_prefetch,
        force_prefetch=args.prima_force_prefetch,
        ctx_size=args.prima_ctx,
        model_file=args.prima_model,
    )


def add_prima_args(parser: argparse.ArgumentParser) -> None:
    """添加 prima.cpp 相关 CLI 参数"""
    g = parser.add_argument_group("Prima.cpp Engine")
    g.add_argument("--engine-type", choices=["ollama", "prima"], default="ollama",
                   help="推理引擎类型 (默认 ollama)")
    g.add_argument("--prima-root", default="/Users/xbowlove/prima.cpp",
                   help="prima.cpp 编译根目录")
    g.add_argument("--prima-model-dir", default="download",
                   help="模型目录 (相对 prima-root)")
    g.add_argument("--prima-role", choices=["head", "worker"], default="head",
                   help="节点角色: head(对外服务) | worker(仅计算)")
    g.add_argument("--prima-world", type=int, default=1,
                   help="分布式总节点数")
    g.add_argument("--prima-rank", type=int, default=0,
                   help="当前节点 rank (0=head)")
    g.add_argument("--prima-master", default="127.0.0.1",
                   help="head 节点 IP")
    g.add_argument("--prima-next", default="127.0.0.1",
                   help="环状拓扑下一个节点 IP")
    g.add_argument("--prima-http-port", type=int, default=8080,
                   help="head 节点 HTTP 服务端口")
    g.add_argument("--prima-data-port", type=int, default=9000,
                   help="数据平面端口")
    g.add_argument("--prima-signal-port", type=int, default=10000,
                   help="信令端口")
    g.add_argument("--prima-gpu-mem", type=int, default=0,
                   help="限制 VRAM 使用 (GB), 0=不限制")
    g.add_argument("--prima-ngl", type=int, default=-1,
                   help="GPU 层数 (-1=全量, 0=纯CPU)")
    g.add_argument("--prima-keep-out-in-cuda", action="store_true",
                   help="输出层放 GPU")
    g.add_argument("--prima-prefetch", action="store_true", default=True,
                   help="启用预取")
    g.add_argument("--prima-force-prefetch", action="store_true",
                   help="强制预取")
    g.add_argument("--prima-ctx", type=int, default=4096,
                   help="上下文长度")
    g.add_argument("--prima-model", default="",
                   help="启动时加载的模型文件名")


import argparse  # 放这里避免循环导入