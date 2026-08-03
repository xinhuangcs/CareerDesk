"""Distribution resources are strict without blocking editable development."""

import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from unittest.mock import patch


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _load_hook_class():
    interface = ModuleType("hatchling.builders.hooks.plugin.interface")
    interface.BuildHookInterface = object
    fake_modules = {
        name: ModuleType(name)
        for name in (
            "hatchling",
            "hatchling.builders",
            "hatchling.builders.hooks",
            "hatchling.builders.hooks.plugin",
        )
    }
    fake_modules[interface.__name__] = interface
    spec = importlib.util.spec_from_file_location(
        "careerdesk_test_hatch_build",
        BACKEND_ROOT / "hatch_build.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, fake_modules):
        spec.loader.exec_module(module)
    return module.CustomBuildHook


CustomBuildHook = _load_hook_class()


class _EditableHook:
    target_name = "wheel"

    def __init__(self, root: Path) -> None:
        self.root = str(root)

    @staticmethod
    def _validate_frontend(_source: Path) -> None:
        raise AssertionError("editable builds must not inspect release frontend files")

    @staticmethod
    def _validate_default_env(_source: Path) -> None:
        raise AssertionError("editable builds must not inspect release default config")


def test_editable_build_does_not_require_prebuilt_release_resources(tmp_path):
    build_data = {"force_include": {}}

    CustomBuildHook.initialize(_EditableHook(tmp_path / "backend"), "editable", build_data)

    assert build_data == {"force_include": {}}
