"""Client for an OpenAI-compatible /chat/completions endpoint."""

from __future__ import annotations

import asyncio
import logging
import re

import httpx

from .config import LLMConfig

log = logging.getLogger(__name__)

_THINK_BLOCK = re.compile(r"<think>(.*?)(?:</think>|$)", re.DOTALL | re.IGNORECASE)
_THINK = re.compile(r"<think>.*?(</think>|$)", re.DOTALL | re.IGNORECASE)


def strip_think(text: str) -> str:
    return _THINK.sub("", text).strip()


class LLMClient:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
        self._http = httpx.AsyncClient(
            base_url=cfg.base_url.rstrip("/"), headers=headers, timeout=cfg.timeout
        )

    async def chat(self, messages: list[dict]) -> str | None:
        """Return the model's reply text, or None if the request failed."""
        body = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
        }
        if self.cfg.reasoning_effort:
            body["reasoning_effort"] = self.cfg.reasoning_effort
        body.update(self.cfg.extra_body)
        for attempt in (1, 2):
            try:
                r = await self._http.post("/chat/completions", json=body)
                r.raise_for_status()
                message = r.json()["choices"][0]["message"]
                content = message.get("content") or ""
                # Servers return thinking either in a separate field or inline as <think>...</think>.
                reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
                inline = "\n".join(m.group(1).strip() for m in _THINK_BLOCK.finditer(content))
                if thinking := "\n".join(t for t in (reasoning.strip(), inline) if t):
                    log.debug("llm thinking: %s", thinking)
                return strip_think(content)
            except httpx.TimeoutException as e:
                # No retry: a local server usually keeps working on the abandoned request, so a retry
                # would queue behind it and answer minutes late. The next message starts a fresh turn.
                log.warning("LLM request timed out after %.0fs (%s), giving up", self.cfg.timeout, type(e).__name__)
                return None
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
                log.warning("LLM request failed (attempt %d): %s: %s", attempt, type(e).__name__, e)
                if attempt == 1:
                    await asyncio.sleep(2)
        return None

    async def aclose(self) -> None:
        await self._http.aclose()
