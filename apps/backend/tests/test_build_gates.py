"""部署包里的前端产物必须是**当前源码**构建出来的。

守的是一个很安静的失败：源码改了，`dist/` 没重建，部署包照样打出来了。
服务端是对的、仓库里是对的，只有浏览器里还是旧字符串 —— 没有任何一步会报错，
因为陈旧的产物本身是一份完全合法的产物。

真实发生过一次：`api/client.ts` 加了一个分支，让 404 显示后端的 `message`
而不是 HTTP 状态行；部署包却是从一个比这次修改还早一小时的 `dist/` 打出来的。
修复在仓库里、在服务端里、就是不在屏幕上。靠端到端 dump 才发现 ——
那种事找到一次是本事，每次都靠它不是。

闸门只比较时间戳：`vite build` 先读 `src/` 再写 `dist/`，所以任何比
`dist/index.html` 新的源文件都意味着产物早于这次改动。
"""

from __future__ import annotations

import importlib.util
import os
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
_SPEC = importlib.util.spec_from_file_location(
    "_build_deploy_gate_under_test", ROOT / "scripts" / "build_deploy.py"
)
assert _SPEC and _SPEC.loader
build_deploy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build_deploy)


def _write(path: Path, *, age_s: float = 0.0) -> Path:
    """Create a file whose mtime is ``age_s`` seconds in the past."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    when = time.time() - age_s
    os.utime(path, (when, when))
    return path


@pytest.fixture
def fake_frontend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A frontend tree with a 600s-old bundle and 1200s-old sources."""
    frontend = tmp_path / "apps" / "frontend"
    _write(frontend / "dist" / "index.html", age_s=600)
    _write(frontend / "src" / "api" / "client.ts", age_s=1200)
    _write(frontend / "index.html", age_s=1200)
    _write(frontend / "package.json", age_s=1200)
    monkeypatch.setattr(build_deploy, "FRONTEND", frontend)
    return frontend


def test_a_fresh_bundle_passes(fake_frontend: Path) -> None:
    assert build_deploy.frontend_staleness(fake_frontend / "dist") is None


def test_a_source_newer_than_the_bundle_is_reported(fake_frontend: Path) -> None:
    _write(fake_frontend / "src" / "pages" / "Home.tsx", age_s=10)

    message = build_deploy.frontend_staleness(fake_frontend / "dist")

    assert message is not None
    assert "stale" in message
    assert "src/pages/Home.tsx" in message, "点名才能修，只说'过期'不行动"
    assert "npm run build" in message, "报错要带上该跑的命令"


def test_the_offending_file_is_the_newest_one(fake_frontend: Path) -> None:
    _write(fake_frontend / "src" / "old.tsx", age_s=120)
    _write(fake_frontend / "src" / "newest.tsx", age_s=5)

    message = build_deploy.frontend_staleness(fake_frontend / "dist")

    assert message is not None
    assert "src/newest.tsx" in message
    assert "old.tsx" not in message


def test_top_level_sources_count_too(fake_frontend: Path) -> None:
    """`index.html` / `package.json` / vite 配置也是构建输入。"""
    _write(fake_frontend / "vite.config.ts", age_s=5)

    message = build_deploy.frontend_staleness(fake_frontend / "dist")

    assert message is not None
    assert "vite.config.ts" in message


def test_files_outside_the_source_set_are_ignored(fake_frontend: Path) -> None:
    """前端的 README 不参与构建，改它不该拦住发布。"""
    _write(fake_frontend / "README.md", age_s=1)
    _write(fake_frontend / "Dockerfile", age_s=1)

    assert build_deploy.frontend_staleness(fake_frontend / "dist") is None


def test_node_modules_is_ignored(fake_frontend: Path) -> None:
    """`src/` 里不会有 node_modules，但真有人放一个进去也不该误报。"""
    _write(fake_frontend / "src" / "node_modules" / "dep" / "index.js", age_s=1)

    assert build_deploy.frontend_staleness(fake_frontend / "dist") is None


def test_a_missing_source_tree_does_not_crash(tmp_path: Path, monkeypatch) -> None:
    """源目录整个不在时返回 None —— 存在性由调用方单独检查。"""
    frontend = tmp_path / "empty-frontend"
    _write(frontend / "dist" / "index.html", age_s=600)
    monkeypatch.setattr(build_deploy, "FRONTEND", frontend)

    assert build_deploy.frontend_staleness(frontend / "dist") is None


def test_the_source_set_excludes_build_output() -> None:
    """否则产物会被拿来和自己比，永远判不出过期。"""
    assert "dist" not in build_deploy.FRONTEND_SOURCES
    assert "node_modules" not in build_deploy.FRONTEND_SOURCES
