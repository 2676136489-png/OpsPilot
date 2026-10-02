"""Screenshot pages at several viewports and measure real rendered contrast.

Two things static CSS review cannot answer, which is why this exists:

1. Does a colour *pair* actually clear WCAG AA? Every number in tokens.css was
   chosen by hand and several were wrong (see --text-on-bright). Reading it back
   from getComputedStyle gives the browser's resolved values, so the ratio here
   is the one a user sees, alpha compositing included.

2. Does a layout survive at 375px? An overflowing grid is invisible to review
   because a stylesheet is not a layout engine.

Speaks real CDP over the WebSocket endpoint rather than the /json/* HTTP
shims: /json/screenshot does not exist (404), and Page.navigate returns before
the page has painted, so a fixed sleep either screenshots a blank frame or
wastes ten seconds per route. Everything here waits on an actual event.

Run:  python ui_probe.py <base_url> <out_dir> [route ...]
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import urllib.parse
import urllib.request

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Users\111\AppData\Local\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

PORT = 9222
VIEWPORTS = [("desktop", 1440, 900), ("tablet", 768, 1024), ("mobile", 375, 812)]

# Pairs that carry meaning in this product, checked against whatever surface
# they actually sit on, resolved at runtime.
#
# Selectors are taken from class names observed in the live DOM, not invented
# from the stylesheet. A first pass guessed names like `.info-label` and
# `.agent-event-desc` for pages that do not use them and reported them as
# `[skip]`, which read as "no problems" while measuring almost nothing.
TARGETS = [
    ("body text", ".page-description, .alert-body, .hypothesis-desc"),
    ("secondary text", ".info-value, .stat-label, .breakdown-label"),
    ("muted text", ".info-label, .metric-sub, .panel-subtitle"),
    ("muted-foreground", ".telemetry-label, .metric-label, .evidence-id"),
    ("primary button", ".btn-primary"),
    ("agent button", ".btn-agent"),
    ("default button", ".btn-default"),
    ("severity critical", ".badge-sev-critical"),
    ("severity high", ".badge-sev-high"),
    ("status resolved", ".badge-status-resolved, .badge-status-completed"),
    ("status failed", ".badge-status-failed"),
    ("live dot", ".live-dot"),
    ("table header", ".data-table thead th"),
    ("table cell", ".data-table tbody td"),
    ("page title", ".page-title"),
    ("metric value", ".metric-value"),
    ("table id cell", ".col-mono"),
    ("filter label", ".filter-group-label"),
    ("topbar clock", ".topbar-clock"),
    ("sidebar label", ".sidebar-group-label"),
]

# The selector list lives in Python but the measuring code runs in the page.
# It has to be injected — referencing TARGETS inside the JS source evaluates to
# a ReferenceError, which Runtime.evaluate hands back as an *error object*, not
# a thrown exception, so the bug surfaces only as a garbage report section.
CONTRAST_JS = r"""
((TARGETS) => {
  const lin = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4) }
  const lum = ([r, g, b]) => 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
  const parse = (s) => (s.match(/[\d.]+/g) || []).slice(0, 4).map(Number)

  // Walk up for the first opaque background. A badge on a card on a page means
  // the effective backdrop is three layers of alpha, not one.
  const backdrop = (el) => {
    let n = el
    while (n && n !== document.documentElement) {
      const bg = parse(getComputedStyle(n).backgroundColor)
      if (bg.length >= 3 && (bg[3] === undefined || bg[3] > 0.95)) return bg.slice(0, 3)
      n = n.parentElement
    }
    return [7, 10, 18]
  }

  const out = []
  for (const [name, sel] of TARGETS) {
    const el = document.querySelector(sel)
    if (!el) { out.push({ name, sel, missing: true }); continue }
    const cs = getComputedStyle(el)
    const fg = parse(cs.color).slice(0, 3)
    const bg = backdrop(el)
    const a = Math.max(lum(fg), lum(bg)), b = Math.min(lum(fg), lum(bg))
    out.push({
      name, sel,
      fg: cs.color,
      bg: `rgb(${bg.join(',')})`,
      size: parseFloat(cs.fontSize),
      weight: cs.fontWeight,
      ratio: Math.round(((a + 0.05) / (b + 0.05)) * 100) / 100,
    })
  }
  return out
})(%s)
""" % json.dumps(TARGETS)

OVERFLOW_JS = r"""
(() => {
  const de = document.documentElement
  // `hidden` and `clip` count as containing, not just the scrollable pair.
  // A service card wider than its panel is clipped by that panel and does not
  // move the document; React Flow pans its own viewport for the same reason.
  // Treating only auto/scroll as containing flagged both as page overflow.
  const contained = (el) => {
    for (let n = el.parentElement; n; n = n.parentElement) {
      const ox = getComputedStyle(n).overflowX
      if (ox === 'auto' || ox === 'scroll' || ox === 'hidden' || ox === 'clip') return true
    }
    return false
  }
  // Only elements that actually stick out past the viewport. The authoritative
  // signal is the document pair below; this list says *where* to look.
  const bad = []
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect()
    if (r.width === 0 || r.height === 0) continue
    if (r.right <= de.clientWidth + 1) continue
    if (contained(el)) continue
    bad.push({
      tag: el.tagName,
      cls: (el.className || '').toString().slice(0, 50),
      right: Math.round(r.right),
    })
    if (bad.length > 6) break
  }
  return {
    docWidth: de.clientWidth,
    scrollWidth: de.scrollWidth,
    // A wide table inside .table-wrap is the intended behaviour — the wrapper
    // scrolls. Counting it as page overflow reported three false BADs on a
    // build whose document scrollWidth equalled clientWidth everywhere.
    overflowing: bad,
    documentOverflows: de.scrollWidth > de.clientWidth + 1,
  }
})()
"""


def find_chrome() -> str | None:
    for p in CHROME_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


# This machine has an intercepting proxy in the process env. Clearing the copy
# handed to a child is not enough: urllib reads getproxies() from our own
# environment, so a loopback request would go through the proxy and come back
# as 502. An explicit empty ProxyHandler bypasses the lookup entirely.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_json(path: str, method: str = "GET", payload=None, timeout: float = 10.0):
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with _OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


class Cdp:
    """Minimal CDP client: send a command, wait for its id, collect events."""

    def __init__(self, ws):
        self.ws = ws
        self.next_id = 0
        self.events: list[dict] = []

    @classmethod
    async def attach(cls, target: dict):
        import websockets

        # Use the URL Chrome hands back rather than rebuilding it from the id.
        # The path shape differs across builds and a hand-built one connects to
        # the wrong target, which surfaces as Page.navigate rejecting a URL
        # that is visibly fine.
        url = target.get("webSocketDebuggerUrl")
        if not url:
            raise RuntimeError(f"no webSocketDebuggerUrl for target {target.get('id')}")
        ws = await websockets.connect(url, max_size=256 * 1024 * 1024, open_timeout=20)
        return cls(ws)

    async def send(self, method: str, params: dict | None = None, timeout: float = 40.0):
        self.next_id += 1
        mid = self.next_id
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
            msg = json.loads(raw)
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            if "method" in msg:
                self.events.append(msg)

    async def wait_event(self, name: str, timeout: float = 25.0):
        for e in self.events:
            if e.get("method") == name:
                self.events.remove(e)
                return e
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=deadline - asyncio.get_event_loop().time())
            except asyncio.TimeoutError:
                return None
            msg = json.loads(raw)
            if msg.get("method") == name:
                return msg
            if "method" in msg:
                self.events.append(msg)
        return None

    async def evaluate(self, expr: str):
        r = await self.send("Runtime.evaluate", {
            "expression": expr, "returnByValue": True, "awaitPromise": True,
        })
        if r.get("exceptionDetails"):
            return {"__error": str(r["exceptionDetails"])[:200]}
        return r.get("result", {}).get("value")

    async def settle(self, rounds: int = 3, pause: float = 0.7):
        """Let fonts, layout and the first data fetch finish.

        Polling document.fonts.ready plus a rAF round is what makes the shot
        deterministic; a fixed sleep is not, because a cold Vite module graph on
        a 2x DPR screenshot takes anywhere from 0.4s to 4s on this machine.
        """
        for _ in range(rounds):
            await self.evaluate(
                "new Promise(r => document.fonts.ready.then(() => requestAnimationFrame(() => setTimeout(r, 60))))"
            )
            await asyncio.sleep(pause)

    async def shot(self) -> bytes:
        r = await self.send("Page.captureScreenshot", {
            "format": "png", "captureBeyondViewport": True, "fromSurface": True,
        })
        return base64.b64decode(r["data"])

    async def close(self):
        try:
            await self.ws.close()
        except Exception:
            pass


async def run(base: str, out_dir: str, routes: list[str], theme: str) -> int:
    report: dict = {"base": base, "theme": theme, "routes": {}}
    for route in routes:
        url = base.rstrip("/") + route
        entry: dict = {"viewports": {}}
        safe = route.strip("/").replace("/", "-") or "root"
        for label, w, h in VIEWPORTS:
            # Create a blank target and navigate afterwards. Passing the real
            # URL through /json/new?<url> needs it percent-encoded, and Chrome
            # does not decode that back into a navigable URL — the target comes
            # back empty and Page.navigate rejects it as invalid.
            tgt = http_json("/json/new?about:blank", "PUT")
            cdp = await Cdp.attach(tgt)
            try:
                await cdp.send("Page.enable")
                await cdp.send("Runtime.enable")
                # Pin the theme on every document. Without this the run inherits
                # whatever localStorage happens to hold, so "verify the light
                # theme" silently measured the dark one — which is how an entire
                # theme shipped with six AA failures nobody looked at.
                await cdp.send("Page.addScriptToEvaluateOnNewDocument", {
                    "source": f"try{{localStorage.setItem('opspilot.theme','{theme}')}}catch(e){{}}",
                })
                await cdp.send("Emulation.setDeviceMetricsOverride", {
                    "width": w, "height": h, "deviceScaleFactor": 2, "mobile": w < 500,
                })
                print(f"    navigating to {url!r} (target {tgt['id'][:8]})")
                await cdp.send("Page.navigate", {"url": url})
                await cdp.wait_event("Page.loadEventFired", timeout=25)
                await cdp.settle()

                # Confirm the theme actually took, so a silent failure here can
                # never again be reported as a passing contrast run.
                applied = await cdp.evaluate("document.documentElement.dataset.theme || 'unset'")
                if applied != theme:
                    raise RuntimeError(f"theme not applied: wanted {theme!r}, page says {applied!r}")

                png = await cdp.shot()
                path = os.path.join(out_dir, f"{safe}-{label}{'' if theme == 'dark' else '-' + theme}.png")
                with open(path, "wb") as f:
                    f.write(png)
                entry["viewports"][label] = {
                    "screenshot": path,
                    "kb": round(len(png) / 1024, 1),
                    "contrast": await cdp.evaluate(CONTRAST_JS),
                    "overflow": await cdp.evaluate(OVERFLOW_JS),
                }
                print(f"  {safe:12s} {label:8s} {len(png)//1024:>5} KB")
            finally:
                await cdp.close()
                try:
                    http_json(f"/json/close/{tgt['id']}", "PUT")
                except Exception:
                    pass
        report["routes"][route] = entry

    with open(os.path.join(out_dir, f"report-{theme}.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"\n=== contrast [{theme}] (AA: 4.5 body / 3.0 large) ===")
    fails = 0
    seen: set[tuple] = set()
    for route, e in report["routes"].items():
        rows = e["viewports"]["desktop"]["contrast"]
        if not isinstance(rows, list):
            # Never let one bad route take the whole report down with it —
            # the screenshots are already on disk at this point.
            print(f"  [bad payload] {route}: {str(rows)[:160]}")
            continue
        for c in rows:
            if c.get("missing"):
                print(f"  [skip] {c['name']} ({c['sel']})")
                continue
            key = (c["name"], c["fg"], c["bg"])
            if key in seen:
                continue
            seen.add(key)
            large = c["size"] >= 18.66 or (c["size"] >= 14 and int(c["weight"]) >= 700)
            need = 3.0 if large else 4.5
            ok = c["ratio"] >= need
            fails += 0 if ok else 1
            print(f"  {'PASS' if ok else 'FAIL'} {c['name']:22s} {c['ratio']:>6}:1 need {need}  "
                  f"{c['size']:.0f}px/{c['weight']}  {c['fg']} on {c['bg']}")
    print(f"\n=== horizontal overflow ===")
    for route, e in report["routes"].items():
        for label, v in e["viewports"].items():
            o = v["overflow"]
            if not isinstance(o, dict) or "__error" in o:
                print(f"  ??  {route:14s} {label:8s} {o}")
                continue
            # The document pair decides. An element wider than the viewport is
            # only a defect when nothing above it clips or scrolls — otherwise
            # it is a wide table in a scroll wrapper, or a card in a panel, and
            # both are the intended layout.
            doc_bad = o.get("documentOverflows", o["scrollWidth"] > o["docWidth"] + 1)
            ok = not doc_bad
            print(f"  {'OK  ' if ok else 'BAD '}{route:14s} {label:8s} "
                  f"doc={o['docWidth']} scroll={o['scrollWidth']}"
                  + (f"  {o.get('overflowing', [])[:2]}" if not ok else ""))
    print(f"\ncontrast failures [{theme}]: {fails}")
    # A run where most selectors missed still prints "0 failures", which reads
    # as a pass. Make the miss rate impossible to overlook.
    total_slots = sum(
        len(e["viewports"]["desktop"]["contrast"])
        for e in report["routes"].values()
        if isinstance(e["viewports"]["desktop"]["contrast"], list)
    )
    print(f"selectors measured: {len(seen)}/{total_slots} "
          f"({len(TARGETS)} defined)")
    if len(seen) < len(TARGETS) * 0.5:
        print("WARNING: over half the selector list never matched — the contrast "
              "result above is not evidence of anything.")
    return fails


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    base = args[0] if len(args) > 0 else "http://127.0.0.1:5175"
    out_dir = args[1] if len(args) > 1 else ".tmp/shots"
    routes = args[2:] or ["/", "/incidents", "/topology", "/evaluations", "/approvals"]
    # Both themes by default. Verifying only the default one leaves the other
    # unchecked, which is exactly how the light palette shipped at 2.77:1.
    themes = ["dark", "light"] if "--both" in flags else [
        "light" if "--light" in flags else "dark"
    ]
    os.makedirs(out_dir, exist_ok=True)

    if not find_chrome():
        print("FATAL: no chromium binary found")
        return 2
    try:
        http_json("/json/version", "GET", timeout=3)
        print(f"reusing chrome on {PORT}")
    except Exception as exc:
        print(f"FATAL: no chrome on {PORT} ({type(exc).__name__}: {exc}). "
              f"Start it first, e.g.\n"
              f'  chrome.exe --headless=new --remote-debugging-port={PORT} '
              f'--remote-allow-origins=* --user-data-dir=.tmp/chrome about:blank')
        return 2

    worst = 0
    for theme in themes:
        rc = asyncio.run(run(base, out_dir, routes, theme))
        worst = max(worst, rc)
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
