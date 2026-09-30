"""The answer key must stay shut unless the harness has asked for it.

``/simulator/ground-truth/{name}`` is the one endpoint that tells the truth
outright: expected root cause, correct recovery, verification criteria. Every
number this project reports is scored against it, and the Agent under test must
never be able to read it. That makes the gate a security boundary, not a
convenience flag — so it gets pinned here.

The gate is also a robustness boundary. It used to be evaluated once, at import
time, which made the answer key's availability depend on *which module imported
the simulator first*. A combined `pytest` run tripped that: the backend suite
imported the app before the harness exported the flag, the route silently never
mounted, and every case scored zero against a 404. Both halves of that — the
route is closed by default, and the flag is honoured whenever it is set — are
asserted below.

Run with::

    python -m pytest evals/opspilot_evals/tests -q
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[3]
for _relative in ("evals", "apps/backend/src", "simulator/src"):
    _path = str(_ROOT / _relative)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from opspilot_simulator.scenarios import scenario_names  # noqa: E402

SCENARIO = "payment-bad-deployment"


@pytest.fixture
def answer_key_client(monkeypatch: pytest.MonkeyPatch):
    """A client bound to the simulator app, with the gate flag under our control.

    The app is imported *before* the flag is touched on purpose: that is the
    import ordering that used to break, and a test that sets the variable first
    would pass against the old code.
    """

    def _build(*, eval_mode: bool) -> httpx.AsyncClient:
        from opspilot_simulator.server import app as sim_app

        if eval_mode:
            monkeypatch.setenv("OPSPILOT_SIM_EVAL_MODE", "1")
        else:
            monkeypatch.delenv("OPSPILOT_SIM_EVAL_MODE", raising=False)
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=sim_app), base_url="http://simulator.test"
        )

    return _build


async def test_answer_key_is_closed_when_eval_mode_is_off(answer_key_client) -> None:
    async with answer_key_client(eval_mode=False) as client:
        response = await client.get(f"/simulator/ground-truth/{SCENARIO}")

    assert response.status_code == 404
    # A plain 404 body, indistinguishable from a route that was never mounted.
    assert "root_cause" not in response.text
    assert "recovery" not in response.text


async def test_answer_key_opens_when_eval_mode_is_on(answer_key_client) -> None:
    async with answer_key_client(eval_mode=True) as client:
        response = await client.get(f"/simulator/ground-truth/{SCENARIO}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["name"] == SCENARIO
    assert payload["hidden_root_cause"]
    assert payload["correct_recovery"]


async def test_gate_follows_the_flag_after_import(answer_key_client) -> None:
    """The flag is re-read per request, so it can be turned on after import.

    This is the regression: with import-time gating, the second request below
    would still be a 404.
    """
    async with answer_key_client(eval_mode=False) as client:
        assert (await client.get(f"/simulator/ground-truth/{SCENARIO}")).status_code == 404

        os.environ["OPSPILOT_SIM_EVAL_MODE"] = "1"
        try:
            assert (await client.get(f"/simulator/ground-truth/{SCENARIO}")).status_code == 200
        finally:
            os.environ.pop("OPSPILOT_SIM_EVAL_MODE", None)

        assert (await client.get(f"/simulator/ground-truth/{SCENARIO}")).status_code == 404


async def test_every_scenario_has_an_answer_key(answer_key_client) -> None:
    """The whole scorecard depends on the catalog and the key agreeing."""

    async with answer_key_client(eval_mode=True) as client:
        response = await client.get("/simulator/ground-truth")

    assert response.status_code == 200
    served = {entry["name"] for entry in response.json()["scenarios"]}
    assert served == set(scenario_names())
