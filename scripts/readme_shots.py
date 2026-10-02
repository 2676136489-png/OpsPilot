"""Capture README screenshots from the running app.

Separate from ui_probe.py on purpose. That one measures contrast and reports
layout overflow, so it screenshots as early as the fonts are ready and takes
three viewports per route. A README wants the opposite: one desktop frame per
page, captured only once the page's own data has landed, because a shot of the
dashboard mid-fetch is a picture of a spinner.

The wait is therefore on content, not on time — see READY_JS for what counts.

Run:
    python scripts/readme_shots.py <base_url> <out_dir> [--theme dark|light]
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ui_probe import Cdp, PORT, find_chrome, http_json  # noqa: E402

# A page counts as ready when it has neither a spinner nor a "加载中" row. Both
# are checked because this app spells "still fetching" two different ways
# depending on which panel is empty: the service cards render a .spinner, and
# the activity list renders a text placeholder.
READY_JS = r"""
(() => {
  const spin = document.querySelectorAll('.spinner, [class*="spinner"]').length
  const loading = (document.body.innerText.match(/加载中|正在加载|构建拓扑|暂无/g) || []).length
  const rows = document.querySelectorAll('.data-table tbody tr').length
  const cards = document.querySelectorAll('.metric-card, .panel').length
  return { spin, loading, rows, cards, text: document.body.innerText.length }
})()
"""

# One frame per page, wide enough to read. 2x DPR on a 1440 viewport produces a
# 2880px PNG; scaled to 1600px wide it is still sharp in a README rendered at
# half width, and roughly a quarter of the bytes.
VIEWPORT = (1600, 1000)
DPR = 2
SCALE_TO = 1600

PAGES = [
    ("dashboard", "/", "指挥中心 — 首屏总览"),
    ("incident", "/incidents/2a913b3c-944f-4dd0-970b-405c6e1fa2cb", "故障详情 — 调查过程与根因"),
    ("incidents", "/incidents", "故障列表 — 筛选与状态"),
    ("agents", "/agents", "Agent 运行 — 步骤与事件"),
    ("evaluations", "/evaluations", "评估 — 场景指标"),
]

# The topology graph derives its edges from a name-prefix convention
# (`payment-service` → `payment`), which none of the seeded service names
# satisfy, so dagre lays out one flat rank of isolated nodes. It is left out
# rather than shipped as a row of boxes with no edges drawn.
ALL_PAGES = PAGES + [
    ("topology", "/topology", "服务拓扑 — 依赖关系"),
]


async def wait_ready(cdp: Cdp, want_rows: int, tries: int = 24) -> dict:
    """Poll until the page's own content is present, or give up and say so.

    Timing out silently is how the previous run shipped screenshots of a
    dashboard reading 「加载服务中…」: the shot was taken, the file was written,
    and nothing recorded that the page had never finished loading.
    """
    last: dict = {}
    for _ in range(tries):
        await cdp.settle(rounds=1, pause=0.5)
        last = await cdp.evaluate(READY_JS) or {}
        if not isinstance(last, dict) or "__error" in last:
            break
        if last.get("spin", 1) == 0 and last.get("loading", 1) == 0:
            if want_rows == 0 or last.get("rows", 0) >= want_rows:
                return last
        await asyncio.sleep(1.0)
    print(f"    WARNING: never became clean — {last}")
    return last


async def capture(base: str, out_dir: str, theme: str) -> int:
    from PIL import Image
    import io

    os.makedirs(out_dir, exist_ok=True)
    # A light-theme run prefixes every name, so the two themes can sit side by
    # side in the same docs/images directory without overwriting each other.
    pages = ALL_PAGES if theme == "dark" else [(s, r, c) for s, r, c in ALL_PAGES if s == "dashboard"]
    for slug, route, _caption in pages:
        name = slug if theme == "dark" else f"{slug}-{theme}"
        url = base.rstrip("/") + route
        tgt = http_json("/json/new?about:blank", "PUT")
        cdp = await Cdp.attach(tgt)
        try:
            await cdp.send("Page.enable")
            await cdp.send("Runtime.enable")
            await cdp.send("Page.addScriptToEvaluateOnNewDocument", {
                "source": f"try{{localStorage.setItem('opspilot.theme','{theme}')}}catch(e){{}}",
            })
            await cdp.send("Emulation.setDeviceMetricsOverride", {
                "width": VIEWPORT[0], "height": VIEWPORT[1],
                "deviceScaleFactor": DPR, "mobile": False,
            })
            await cdp.send("Page.navigate", {"url": url})
            await cdp.wait_event("Page.loadEventFired", timeout=30)

            applied = await cdp.evaluate("document.documentElement.dataset.theme || 'unset'")
            if applied != theme:
                raise RuntimeError(f"theme not applied: wanted {theme!r}, got {applied!r}")

            # List pages need at least one data row before they show anything
            # worth putting in a README; the dashboard and the detail page are
            # fine with their panels alone.
            want = 1 if route.rstrip("/").count("/") >= 1 and route != "/" else 0
            state = await wait_ready(cdp, want)

            png = await cdp.shot()
            img = Image.open(io.BytesIO(png))
            raw_bytes = len(png)
            if img.width > SCALE_TO:
                img = img.resize(
                    (SCALE_TO, round(img.height * SCALE_TO / img.width)),
                    Image.LANCZOS,
                )
            # Palette-quantised PNG: these are flat UI fills with text, which is
            # exactly what an 8-bit palette handles well. Keeps the screenshots
            # legible in a repo instead of adding megabytes of truecolour noise.
            out = img.convert("P", palette=Image.ADAPTIVE, colors=128)
            path = os.path.join(out_dir, f"{name}.png")
            out.save(path, optimize=True)

            state_txt = (state.get("text") if isinstance(state, dict) else "?")
            print(f"  {name:18s} {img.width}x{img.height}  "
                  f"{raw_bytes//1024}KB -> {os.path.getsize(path)//1024}KB  "
                  f"text={state_txt}")
        finally:
            await cdp.close()
            try:
                http_json(f"/json/close/{tgt['id']}", "PUT")
            except Exception:
                pass
    return 0


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    base = args[0] if args else "http://127.0.0.1:5175"
    out_dir = args[1] if len(args) > 1 else "docs/images"
    theme = "light" if "--light" in flags else "dark"
    if not find_chrome():
        print("FATAL: no chromium binary found")
        return 2
    try:
        http_json("/json/version", "GET", timeout=3)
    except Exception as exc:
        print(f"FATAL: no chrome on {PORT} ({type(exc).__name__}: {exc})")
        return 2
    return asyncio.run(capture(base, out_dir, theme))


if __name__ == "__main__":
    raise SystemExit(main())
