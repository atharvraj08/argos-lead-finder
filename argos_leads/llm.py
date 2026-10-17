"""Claude helpers. Two uses only:
  - extract(): pull structured fields out of page text we already fetched.
  - search(): run a targeted web search (scalping history, competitor evidence) and
    return a structured answer with source URLs.
Requires ANTHROPIC_API_KEY. Refusal fallbacks are enabled server-side.
"""
from __future__ import annotations

import logging
import os
from typing import TypeVar

import anthropic
from pydantic import BaseModel

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search"}
MAX_TEXT_CHARS = 150_000  # page text sent per extraction call


class LLM:
    def __init__(self, model: str = "claude-opus-5-5", effort: str = "low", client: anthropic.Anthropic | None = None):
        self.model = model
        self.effort = effort
        self._client = client
        self._warned = False

    @property
    def available(self) -> bool:
        """False when no Anthropic credentials are configured; callers then get None."""
        return self._client is not None or bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic()
        return self._client

    def _parse(self, output: type[T], system: str, content: str, tools: list | None = None, max_uses: int = 0) -> T | None:
        if not self.available:
            if not self._warned:
                log.warning("ANTHROPIC_API_KEY not set: LLM extraction and search are skipped")
                self._warned = True
            return None
        kwargs = {}
        if tools:
            kwargs["tools"] = [dict(t, max_uses=max_uses) if max_uses else t for t in tools]
        try:
            resp = self.client.beta.messages.parse(
                model=self.model,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": content}],
                output_format=output,
                output_config={"effort": self.effort},
                betas=[FALLBACK_BETA],
                fallbacks="default",
                **kwargs,
            )
        except anthropic.APIStatusError as e:
            log.error("llm call failed: %s %s", e.status_code, e.message)
            return None
        except anthropic.APIConnectionError as e:
            log.error("llm connection failed: %s", e)
            return None
        if resp.stop_reason == "refusal":
            log.warning("llm refused request")
            return None
        return resp.parsed_output

    def extract(self, output: type[T], instructions: str, page_text: str, url: str = "") -> T | None:
        """Extract fields from fetched page text. Never invent values: the system prompt
        tells the model to leave fields empty when the text does not state them."""
        system = (
            "You extract facts from the provided web page text into the given schema. "
            "Use only what the text explicitly states. If a field is not stated, leave it empty "
            "or null. Never guess or use outside knowledge. Dates as YYYY-MM-DD."
        )
        text = page_text[:MAX_TEXT_CHARS]
        content = f"{instructions}\n\nSource URL: {url}\n\n<page_text>\n{text}\n</page_text>"
        return self._parse(output, system, content)

    def search(self, output: type[T], question: str, max_searches: int = 2) -> T | None:
        """Answer a question using web search; the schema should include source URL fields."""
        system = (
            "You research with web search and answer in the given schema. Only report findings "
            "backed by a page you found; include that page's URL. Paraphrase in one short line; "
            "never copy article text. If nothing relevant is found, leave fields empty."
        )
        return self._parse(output, system, question, tools=[WEB_SEARCH_TOOL], max_uses=max_searches)
