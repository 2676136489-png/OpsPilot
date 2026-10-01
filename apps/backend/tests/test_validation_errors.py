"""The prose FastAPI would otherwise answer with.

An invalid path parameter comes back as ``{"detail": [{"msg": "Input should be
a valid UUID, ..."}]}`` — pydantic's wording, in English, and shaped as a list
rather than a string. The SPA has a branch in ``api/client.ts`` that joins those
``msg`` fields and renders them, so this text has a route to the screen; it is
just not a route the UI takes on its own, which is exactly how one English
sentence survives in a Chinese interface.

The handler replaces the prose and keeps the structure, because the structure is
the diagnostic: ``type`` and ``location`` are what identify the offending field,
and translating them would delete the information rather than soften it.
"""

from __future__ import annotations

from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from opspilot_backend.main import _validation_error_handler


@pytest.fixture
async def client() -> httpx.AsyncClient:
    """A minimal app carrying the real handler — not the full app.

    The full app's lifespan checks and may rebuild a database on disk, which a
    test of a response shape has no business doing.
    """
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, _validation_error_handler)

    @app.get("/things/{thing_id}")
    async def _get_thing(thing_id: UUID) -> dict[str, str]:  # pragma: no cover
        return {"thing_id": str(thing_id)}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://probe"
    ) as http:
        yield http


async def test_a_malformed_path_parameter_is_answered_in_chinese(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/things/not-a-uuid")

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, str), "the SPA prefers a string and renders it verbatim"
    assert detail.startswith("请求参数不合法")
    assert "path.thing_id" in detail, "the offending field is what makes it actionable"


async def test_the_diagnostics_survive_in_machine_readable_form(
    client: httpx.AsyncClient,
) -> None:
    payload = (await client.get("/things/not-a-uuid")).json()

    assert payload["errors"], "dropping these would trade a translation for a blind spot"
    assert payload["errors"][0]["type"] == "uuid_parsing"
    assert "thing_id" in payload["errors"][0]["location"]


async def test_a_valid_request_is_untouched(client: httpx.AsyncClient) -> None:
    response = await client.get("/things/3f2504e0-4f89-11d3-9a0c-0305e82c3301")

    assert response.status_code == 200
    assert response.json() == {"thing_id": "3f2504e0-4f89-11d3-9a0c-0305e82c3301"}
