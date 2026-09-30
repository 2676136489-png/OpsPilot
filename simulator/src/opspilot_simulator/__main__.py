"""OpsPilot Incident Simulator — CLI entry point.

Run with:
    python -m opspilot_simulator

Which is equivalent to:
    uvicorn opspilot_simulator.server:app --host 0.0.0.0 --port 9000
"""

from __future__ import annotations

import os


def _ensure_simulator_importable() -> None:
    # Nothing required — package is installed or on PYTHONPATH.
    pass


def main() -> None:
    import uvicorn

    host = os.environ.get("OPSPILOT_SIM_HOST", "0.0.0.0")
    port = int(os.environ.get("OPSPILOT_SIM_PORT", "9000"))

    uvicorn.run(
        "opspilot_simulator.server:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
