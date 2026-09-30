"""LLM provider seam.

The agent's *decisions* never come from a prompt — evidence drives them. The
LLM is only asked to do what deterministic code does badly: phrase a
hypothesis in prose and write a postmortem narrative. When no key is
configured the deterministic provider is used and the run reports
``reasoning_mode="deterministic"`` rather than pretending a model answered.

Every reply carries its own token counts. Two nodes can call the model
concurrently inside one run, so the spend has to travel with the reply that
incurred it instead of through a shared counter — and
:func:`complete_charged` is what turns those counts into the
``AgentRun.spent_tokens`` the evaluation report measures.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from opspilot_backend.agent.budget import Budget
from opspilot_backend.core.config import get_settings
from opspilot_backend.core.tracing import aspan

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Completion:
    """One model reply and what it cost."""

    text: str | None = None
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __bool__(self) -> bool:
        """Truthy exactly when the model actually said something."""
        return bool(self.text)


class Charged(Protocol):
    """Anything owning an investigation budget — in practice ``NodeContext``.

    Structural, not a base class: the agent layer must stay importable without
    dragging the context module in, and a test can pass a two-field stub.
    """

    budget: Budget

    async def persist_budget(self) -> None: ...


class LLMProvider(Protocol):
    name: str

    async def complete(self, system: str, user: str) -> Completion: ...


class DeterministicProvider:
    """No network, no key, no invented output — returns nothing on purpose."""

    name = "deterministic"

    async def complete(self, system: str, user: str) -> Completion:
        return Completion(model=self.name)


class OpenAICompatibleProvider:
    """Works with OpenAI, GLM, DeepSeek, vLLM… anything chat-completions shaped."""

    name = "openai_compatible"

    def __init__(self) -> None:
        settings = get_settings()
        self._api_key = settings.openai_api_key
        self._base_url = settings.openai_base_url.rstrip("/")
        self._model = settings.openai_model
        self._temperature = settings.openai_temperature
        self._timeout = settings.openai_timeout

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    async def complete(self, system: str, user: str) -> Completion:
        import httpx

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model,
            "temperature": self._temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        # GLM-4-flash (free tier) can be slow to start; the default is generous
        # without letting a stuck request block the whole incident window.
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                response = await client.post(
                    f"{self._base_url}/chat/completions", headers=headers, json=payload
                )
            except httpx.TimeoutException as exc:
                logger.warning("llm.timeout model=%s error=%s", self._model, exc)
                return Completion(model=self._model)
            except httpx.HTTPError as exc:
                logger.warning("llm.http_error model=%s error=%s", self._model, exc)
                return Completion(model=self._model)
        if response.status_code >= 400:
            logger.warning("llm.error_status model=%s status=%s", self._model, response.status_code)
            return Completion(model=self._model)
        body = response.json()
        choices = body.get("choices") or []
        if not choices:
            logger.warning("llm.empty_choices model=%s", self._model)
            return Completion(model=self._model)
        # ``usage`` is a documented part of the response, but a self-hosted
        # gateway may omit it — missing counts are reported as zero rather than
        # guessed from a character count, because a fabricated metric is worse
        # than an absent one.
        usage = body.get("usage") or {}
        return Completion(
            text=choices[0].get("message", {}).get("content"),
            model=str(body.get("model") or self._model),
            prompt_tokens=_as_int(usage.get("prompt_tokens")),
            completion_tokens=_as_int(usage.get("completion_tokens")),
        )


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


_provider: LLMProvider | None = None


def get_llm() -> LLMProvider:
    global _provider
    if _provider is None:
        settings = get_settings()
        mode = (settings.llm_provider or "auto").lower()
        if mode in {"auto", "openai", "openai_compatible"}:
            candidate = OpenAICompatibleProvider()
            _provider = candidate if candidate.configured else DeterministicProvider()
        else:
            _provider = DeterministicProvider()
    return _provider


def set_llm(provider: LLMProvider | None) -> None:
    global _provider
    _provider = provider


async def complete_charged(
    llm: LLMProvider,
    ctx: Charged,
    *,
    purpose: str,
    system: str,
    user: str,
) -> Completion:
    """Ask the model, trace the call, and charge the spend to the run budget.

    The span is opened here rather than inside a provider because this is the
    only place that knows *why* the model was called — a trace that says
    ``llm.complete`` without ``purpose=postmortem`` answers the wrong question.
    Charging here is likewise the only way ``AgentRun.spent_tokens`` can be
    anything other than zero.
    """
    async with aspan(
        "llm.complete", kind="llm", provider=llm.name, purpose=purpose
    ) as sp:
        try:
            reply = await llm.complete(system, user)
        except Exception as exc:
            # LLM calls are prose-only: a failure must never break the
            # evidence-driven decision loop. Fall back to deterministic output.
            logger.warning("llm.complete_failed provider=%s purpose=%s error=%s", llm.name, purpose, exc)
            reply = Completion(model=llm.name)
        sp.attribute("model", reply.model or llm.name)
        sp.attribute("prompt_tokens", reply.prompt_tokens)
        sp.attribute("completion_tokens", reply.completion_tokens)
        sp.attribute("total_tokens", reply.total_tokens)
        sp.attribute("answered", bool(reply.text))
        sp.attribute("prompt_chars", len(system) + len(user))

    if reply.total_tokens:
        ctx.budget.record_tokens(reply.total_tokens)
        await ctx.persist_budget()
    return reply


def parse_json_object(text: str | None) -> dict[str, Any] | list[Any] | None:
    """Best-effort JSON extraction from a model reply.

    Returns a list too: the hypothesis node asks for a JSON array and a
    dict-only parser would silently drop every refinement it produced.
    """
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    start = min(
        (i for i in (cleaned.find("{"), cleaned.find("[")) if i != -1),
        default=-1,
    )
    end = max(cleaned.rfind("}"), cleaned.rfind("]"))
    if start == -1 or end == -1:
        return None
    try:
        parsed = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, (dict, list)) else None


__all__ = [
    "Charged",
    "Completion",
    "DeterministicProvider",
    "LLMProvider",
    "OpenAICompatibleProvider",
    "complete_charged",
    "get_llm",
    "parse_json_object",
    "set_llm",
]
