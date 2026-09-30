"""Single-port ASGI entrypoint for hosting platforms.

Hosting sandboxes give the app one HTTP port and do not necessarily install the
package into the environment, so ``src/`` has to be placed on ``sys.path``
before the application module can be imported at all.

Run:
    python serve.py                  # honours $PORT, defaults to 8000
    uvicorn serve:app --port $PORT
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent

# Nothing may depend on the caller's working directory. Two things resolve
# relative to it and break silently when a hosting platform starts the server
# from somewhere else: `.env` (so no LLM key and no embedded simulator) and the
# SQLite URL `sqlite+aiosqlite:///./opspilot.db` (so every restart opens a
# different, empty database). Both degrade instead of failing loudly, which is
# how a bad deployment survives its own smoke test.
#
# Anchoring here keeps the fix inside the unit that got uploaded: in the deploy
# tree this file sits next to `.env` and the database by construction, whatever
# working directory the platform happens to pick.
os.chdir(_ROOT)

sys.path.insert(0, str(_ROOT / "src"))

from opspilot_backend.main import app  # noqa: E402

__all__ = ["app"]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", 8000)),
    )
