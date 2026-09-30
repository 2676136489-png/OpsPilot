"""Assemble `deploy/` — the single-port unit that gets published.

The repo itself cannot be uploaded as-is:

* `apps/backend/.venv` is ~97 MB of local virtualenv.
* `apps/frontend/dist/` is build output and is git-ignored (publishing tools
  skip build output too).
* The repo root carries a `docker-compose.yml` declaring Postgres and Redis,
  which the hosting sandbox does not provide.

So this script copies only what the running service needs and renames `dist/`
to `webroot/` along the way, which is both the path the backend looks for and
a name no build-output filter recognises.

Usage:
    python scripts/build_deploy.py
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "apps" / "backend"
FRONTEND = ROOT / "apps" / "frontend"
DEPLOY = ROOT / "deploy"

# `.env` is read by pydantic-settings from the process working directory, which
# is the deploy root — this is how demo seeding is enabled for the hosted
# instance only, without touching local behaviour or tests.
BASE_ENV = """APP_ENV=production
APP_DEBUG=false
OPSPILOT_DEMO_SEED=true
# Hosting targets expose ONE port. Without this the provider client dials the
# default http://127.0.0.1:8100 for a companion simulator process that does not
# exist in the sandbox, and every /api/v1/services call 500s. With it the
# simulator is mounted in-process at /__sim and repointed at this port (see the
# bootstrap block at the top of opspilot_backend.main).
OPSPILOT_EMBED_SIMULATOR=true
"""

# Forwarded from the operator's own shell when present, never written here.
# Keeping them out of the source tree is the point: no key ever enters git.
# `deploy/` is git-ignored, so the value only lands in the upload unit.
FORWARDED_ENV = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "OPSPILOT_LLM_PROVIDER",
    "OPENAI_TEMPERATURE",
)


def build_dotenv() -> str:
    lines = [BASE_ENV.rstrip("\n")]
    for name in FORWARDED_ENV:
        value = os.environ.get(name, "").strip()
        if value:
            lines.append(f"{name}={value}")
    return "\n".join(lines) + "\n"


def copy_tree(src: Path, dest: Path) -> int:
    """Copy `src` into `dest`, skipping caches. Returns the file count."""
    count = 0
    for item in src.rglob("*"):
        if any(p in {"__pycache__", ".pytest_cache", ".ruff_cache"} for p in item.parts):
            continue
        if item.is_dir():
            continue
        target = dest / item.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        count += 1
    return count


def main() -> int:
    dist = FRONTEND / "dist"
    if not (dist / "index.html").is_file():
        print("error: frontend not built — run `npm run build` first", file=sys.stderr)
        return 1

    if DEPLOY.exists():
        shutil.rmtree(DEPLOY)
    DEPLOY.mkdir(parents=True)

    n_src = copy_tree(BACKEND / "src", DEPLOY / "src")
    n_web = copy_tree(dist, DEPLOY / "webroot")

    # Runbooks are read from disk at request time, so they have to ship with the
    # unit — otherwise the Runbooks page and the agent's runbook_search tool
    # both find nothing. Because the deploy tree is one level shallower than the
    # source tree, they also have to sit where the lookup walk can find them.
    n_run = 0
    runbooks = ROOT / "runbooks"
    if runbooks.is_dir():
        n_run = copy_tree(runbooks, DEPLOY / "runbooks")

    # pyproject.toml is deliberately left out: it declares the `uv_build`
    # backend, and if the host picks it up it will try to *build* the package
    # instead of just installing the pinned requirements.
    for name in ("serve.py", "requirements.txt"):
        if (BACKEND / name).is_file():
            shutil.copy2(BACKEND / name, DEPLOY / name)

    db = BACKEND / "opspilot.db"
    if db.is_file():
        shutil.copy2(db, DEPLOY / "opspilot.db")

    (DEPLOY / ".env").write_text(build_dotenv(), encoding="utf-8")

    forwarded = [n for n in FORWARDED_ENV if os.environ.get(n, "").strip()]
    print(f"deploy/ rebuilt — src {n_src} files, webroot {n_web} files, runbooks {n_run} files")
    if forwarded:
        print(f"LLM config forwarded from this shell: {', '.join(forwarded)}")
    else:
        print("no LLM key in this shell — agent will run deterministic (export OPENAI_API_KEY first to enable it)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
