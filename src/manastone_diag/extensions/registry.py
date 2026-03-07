"""运行时扩展加载与注册。"""

from __future__ import annotations

import importlib
import inspect
import logging
import os
from dataclasses import dataclass
from importlib import metadata
from typing import Any, Callable, Iterable

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)


RegisterFn = Callable[[FastMCP], Any]


@dataclass
class LoadedExtension:
    """已加载扩展信息。"""

    module_name: str
    register_fn: RegisterFn


class ExtensionRegistry:
    """根据环境变量和 entry points 加载并注册 extension。"""

    def __init__(
        self,
        env_var: str = "MANASTONE_EXTENSIONS",
        entrypoint_group: str = "manastone_diag.extensions",
    ):
        self.env_var = env_var
        self.entrypoint_group = entrypoint_group

    def discover_modules(self) -> list[str]:
        """解析 MANASTONE_EXTENSIONS 的扩展模块列表。"""
        raw = os.getenv(self.env_var, "")
        if not raw.strip():
            return []
        return [m.strip() for m in raw.split(",") if m.strip()]

    def discover_entrypoint_modules(self) -> list[str]:
        """发现通过 Python entry points 安装的扩展模块。"""
        eps = metadata.entry_points()
        # py310/py311 API 兼容
        if hasattr(eps, "select"):
            items = list(eps.select(group=self.entrypoint_group))
        else:
            items = list(eps.get(self.entrypoint_group, []))

        modules = [ep.value for ep in items if ep.value]
        return modules

    @staticmethod
    def _resolve_register(module_spec: str) -> LoadedExtension:
        """支持 module 或 module:attr 形式，并提取 register callable。"""
        if ":" in module_spec:
            module_name, attr_name = module_spec.split(":", 1)
            module = importlib.import_module(module_name)
            register = getattr(module, attr_name, None)
        else:
            module = importlib.import_module(module_spec)
            register = getattr(module, "register", None)

        if not callable(register):
            raise ValueError(f"扩展模块 {module_spec} 缺少可调用注册函数")

        return LoadedExtension(module_name=module_spec, register_fn=register)

    def load_extensions(self, module_names: Iterable[str] | None = None) -> list[LoadedExtension]:
        """导入模块并提取注册函数。"""
        names = list(module_names) if module_names is not None else self.discover_modules()
        loaded: list[LoadedExtension] = []

        for module_name in names:
            loaded.append(self._resolve_register(module_name))

        return loaded

    async def register_extensions(
        self,
        server: FastMCP,
        module_names: Iterable[str] | None = None,
    ) -> list[str]:
        """加载并注册扩展，返回已注册模块名。"""
        explicit = list(module_names) if module_names is not None else self.discover_modules()
        automatic = self.discover_entrypoint_modules()

        deduped_names: list[str] = []
        for name in [*explicit, *automatic]:
            if name not in deduped_names:
                deduped_names.append(name)

        loaded_names: list[str] = []
        for module_name in deduped_names:
            try:
                ext = self._resolve_register(module_name)
                result = ext.register_fn(server)
                if inspect.isawaitable(result):
                    await result
                loaded_names.append(ext.module_name)
                logger.info("✅ Extension 已注册: %s", ext.module_name)
            except Exception as e:
                logger.exception("❌ Extension 加载失败 %s: %s", module_name, e)

        return loaded_names
