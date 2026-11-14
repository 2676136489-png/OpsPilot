"""Capture README screenshots from the running app.

The UI is dark-only, so there is one frame per page. Each frame waits for the
page's own data to land; a screenshot of a loading state is not documentation.

Run:
    python scripts/readme_shots.py <base_url> <out_dir>

The Chrome instance must already expose CDP on PORT (see ui_probe.py).
"""

from __future__ import annotations

import asyncio
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ui_probe import Cdp, PORT, find_chrome, http_json  # noqa: E402

READY_JS = r"""
(() => {
  const spin = document.querySelectorAll('.spinner, [class*="spinner"]').length
  const loading = (document.body.innerText.match(/加载中|正在加载|构建拓扑/g) || []).length
  const rows = document.querySelectorAll('.data-table tbody tr').length
  const cards = document.querySelectorAll('.metric-card, .panel').length
  return { spin, loading, rows, cards, text: document.body.innerText.length }
})()
"""

WIDTH = 1600
DPR = 1
PAGES = [
    ("dashboard", "/", 1200),
    ("incidents", "/incidents", 1000),
    ("capabilities", "/capabilities", 1600),
    ("capability-diagnosis", "/capabilities/diagnosis", 1200),
    ("evaluations", "/evaluations", 1900),
    ("runbooks", "/runbooks", 1200),
]


async def wait_ready(cdp: Cdp, want_rows: int, tries: int = 24) -> dict:
    last: dict = {}
    for _ in range(tries):
        await cdp.settle(rounds=1, pause=0.5)
        last = await cdp.evaluate(READY_JS) or {}
        if not isinstance(last, dict) or "__error__" in last:
            break
        if last.get("spin", 1) == 0 and last.get("loading", 1) == 0:
            if want_rows == 0 or last.get("rows", 0) >= want_rows:
                return last
        await asyncio.sleep(1.0)
    print(f"    WARNING: page did not settle — {last}")
    return last


async def capture(base: str, out_dir: str) -> int:
    from PIL import Image

    os.makedirs(out_dir, exist_ok=True)
    for slug, route, height in PAGES:
        target = http_json("/json/new?about:blank", "PUT")
        cdp = await Cdp.attach(target)
        try:
            await cdp.send("Page.enable")
            await cdp.send("Runtime.enable")
            await cdp.send(
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": WIDTH,
                    "height": height,
                    "deviceScaleFactor": DPR,
                    "mobile": False,
                },
            )
            await cdp.send("Page.navigate", {"url": base.rstrip("/") + route})
            await cdp.wait_event("Page.loadEventFired", timeout=30)

            want_rows = 1 if slug in {"incidents", "runbooks"} else 0
            state = await wait_ready(cdp, want_rows)

            png = await cdp.shot()
            image = Image.open(io.BytesIO(png)).convert("RGB")
            if image.width > WIDTH:
                image = image.resize((WIDTH, round(image.height * WIDTH / image.width)), Image.LANCZOS)
            path = os.path.join(out_dir, f"{slug}.png")
            image.convert("P", palette=Image.ADAPTIVE, colors=128).save(path, optimize=True)

            text_size = state.get("text", "?") if isinstance(state, dict) else "?"
            print(
                f"  {slug:20s} {image.width}x{image.height}  "
                f"{os.path.getsize(path) // 1024}KB  text={text_size}"
            )
        finally:
            await cdp.close()
            try:
                http_json(f"/json/close/{target['id']}", "PUT")
            except Exception:
                pass
    return 0


def main() -> int:
    args = [item for item in sys.argv[1:] if not item.startswith("--")]
    base = args[0] if args else "http://127.0.0.1:8010"
    out_dir = args[1] if len(args) > 1 else "docs/images"
    if not find_chrome():
        print("FATAL: no chromium binary found")
        return 2
    try:
        http_json("/json/version", "GET", timeout=3)
    except Exception as exc:
        print(f"FATAL: no Chrome on {PORT} ({type(exc).__name__}: {exc})")
        return 2
    return asyncio.run(capture(base, out_dir))


if __name__ == "__main__":
    raise SystemExit(main())
