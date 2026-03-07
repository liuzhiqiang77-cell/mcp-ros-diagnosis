import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from manastone_diag.extensions import registry as registry_module
from manastone_diag.extensions import ExtensionRegistry


class _DummyEP:
    def __init__(self, value: str):
        self.value = value


class _DummyEPCollection:
    def __init__(self, items):
        self._items = items

    def select(self, group: str):
        if group == "manastone_diag.extensions":
            return self._items
        return []


def test_extension_registry_discovery(monkeypatch):
    monkeypatch.setenv(
        "MANASTONE_EXTENSIONS",
        "manastone_diag.extensions.demo_extension",
    )
    registry = ExtensionRegistry()
    assert registry.discover_modules() == ["manastone_diag.extensions.demo_extension"]


def test_extension_registry_entrypoint_discovery(monkeypatch):
    monkeypatch.setattr(
        registry_module.metadata,
        "entry_points",
        lambda: _DummyEPCollection([_DummyEP("manastone_diag.extensions.demo_extension")]),
    )
    registry = ExtensionRegistry()
    assert registry.discover_entrypoint_modules() == ["manastone_diag.extensions.demo_extension"]


def test_extension_registry_load_with_attr():
    registry = ExtensionRegistry()
    loaded = registry.load_extensions(["manastone_diag.extensions.demo_extension:register"])
    assert len(loaded) == 1
    assert loaded[0].module_name == "manastone_diag.extensions.demo_extension:register"
    assert callable(loaded[0].register_fn)


def test_extension_registry_register_extensions(monkeypatch):
    monkeypatch.setattr(
        registry_module.metadata,
        "entry_points",
        lambda: _DummyEPCollection([_DummyEP("manastone_diag.extensions.demo_extension")]),
    )
    registry = ExtensionRegistry()

    class DummyServer:
        def tool(self, *args, **kwargs):
            def deco(fn):
                return fn

            return deco

        def resource(self, *args, **kwargs):
            def deco(fn):
                return fn

            return deco

    loaded = asyncio.run(registry.register_extensions(DummyServer(), []))
    assert loaded == ["manastone_diag.extensions.demo_extension"]
