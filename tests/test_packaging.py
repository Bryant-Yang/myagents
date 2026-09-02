"""源码级全局命令的打包契约。"""

from __future__ import annotations

import importlib
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_console_scripts_resolve_to_callable_source_entries() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    scripts = config["project"]["scripts"]
    assert scripts == {
        "myagents": "main:main",
        "myagents-mcp": "myagents_mcp:main",
    }
    for target in scripts.values():
        module_name, attribute = target.split(":", 1)
        entry = getattr(importlib.import_module(module_name), attribute)
        assert callable(entry)


def test_setuptools_declares_every_source_module_and_package() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    setuptools = config["tool"]["setuptools"]
    expected_modules = {path.stem for path in ROOT.glob("*.py")}
    assert set(setuptools["py-modules"]) == expected_modules

    expected_packages = {
        f"{path.parent.name}*"
        for path in ROOT.glob("*/__init__.py")
        if path.parent.name != "tests"
    }
    assert set(setuptools["packages"]["find"]["include"]) \
        == expected_packages


def test_dsh_bundle_resources_are_part_of_the_distribution() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    patterns = set(
        config["tool"]["setuptools"]["package-data"]["dsh_acp"])
    assert {
        "plugin/*.json",
        "plugin/*.yml",
        "plugin/src/*.ts",
    } <= patterns
    assert set(
        config["tool"]["setuptools"]["package-data"]["remote_control"]
    ) == {"static/*.html", "static/*.js"}


if __name__ == "__main__":
    test_console_scripts_resolve_to_callable_source_entries()
    print("ok  全局命令入口可导入")
    test_setuptools_declares_every_source_module_and_package()
    print("ok  顶层模块与 package 声明完整")
    test_dsh_bundle_resources_are_part_of_the_distribution()
    print("ok  DSH bundle 资源进入 distribution")
    print("\n源码级全局命令打包契约全部通过")
