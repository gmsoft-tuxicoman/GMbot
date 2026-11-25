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
_CLOSE = "</think>"


def split_think(text: str) -> tuple[str, str]:
    """(thinking, answer) from a reply that may contain <think>...</think> blocks.

    Qwen-style chat templates put the opening <think> in the prompt, so the reply may only have the
    closing tag: everything before the last lone </think> is thinking too.
    """
    thinking = [m.group(1).strip() for m in _THINK_BLOCK.finditer(text)]
    text = _THINK.sub("", text)
    if (i := text.lower().rfind(_CLOSE)) >= 0:
        thinking.append(text[:i].strip())
        text = text[i + len(_CLOSE) :]
    return "\n".join(t for t in thinking if t), text.strip()


def strip_think(text: str) -> str:
    return split_think(text)[1]


class LLMClient:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
        self._http = httpx.AsyncClient(
            base_url=cfg.base_url.rstrip("/"), headers=headers, timeout=cfg.timeout
        )

    async def chat(
        self,
        messages: list[dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> str | None:
        """Return the model's reply text, or None if the request failed. Keyword arguments override
        the config for this request."""
        body = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.cfg.max_tokens,
        }
        effort = self.cfg.reasoning_effort if reasoning_effort is None else reasoning_effort
        if effort:
            body["reasoning_effort"] = effort
        body.update(self.cfg.extra_body)
        for attempt in (1, 2):
            try:
                r = await self._http.post("/chat/completions", json=body)
                r.raise_for_status()
                message = r.json()["choices"][0]["message"]
                content = message.get("content") or ""
                # Servers return thinking either in a separate field or inline as <think>...</think>.
                reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
                inline, answer = split_think(content)
                if thinking := "\n".join(t for t in (reasoning.strip(), inline) if t):
                    log.debug("llm thinking: %s", thinking)
                return answer
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
