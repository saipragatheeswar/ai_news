"""Client for a locally hosted Ollama model (kept small enough to run on CPU)."""

from __future__ import annotations

import json
import logging
import re
import time

import requests
from django.conf import settings

logger = logging.getLogger("news.llm")


class LocalModelError(RuntimeError):
    pass


class LocalModel:
    """Chat wrapper around Ollama's HTTP API."""

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        timeout: int | None = None,
    ):
        self.model = model or settings.OLLAMA_MODEL
        self.base_url = (base_url or settings.OLLAMA_BASE_URL).rstrip("/")
        self.timeout = timeout or settings.OLLAMA_TIMEOUT

    def health_check(self) -> None:
        try:
            response = requests.get(f"{self.base_url}/api/tags", timeout=10)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise LocalModelError(
                f"Cannot reach Ollama at {self.base_url}. Is `ollama serve` running? ({exc})"
            ) from exc

        installed = {m.get("name", "") for m in response.json().get("models", [])}
        bare = {name.split(":")[0] for name in installed}
        if self.model not in installed and self.model.split(":")[0] not in bare:
            raise LocalModelError(
                f"Model {self.model!r} is not installed. Run: ollama pull {self.model}"
            )

    def chat(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.7,
        json_mode: bool = False,
        max_retries: int = 2,
        num_predict: int | None = None,
    ) -> str:
        payload = {
            "model": self.model,
            "stream": False,
            # Without this the model is evicted between our (slow) calls and
            # every request pays a 30-60s reload. num_ctx must also stay
            # constant across calls, since changing it forces a reload too.
            "keep_alive": settings.OLLAMA_KEEP_ALIVE,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "options": {
                "temperature": temperature,
                "num_ctx": settings.OLLAMA_NUM_CTX,
                "num_predict": num_predict or settings.OLLAMA_NUM_PREDICT,
                "top_p": 0.9,
                "repeat_penalty": 1.1,
            },
        }
        if json_mode:
            payload["format"] = "json"

        last_error: Exception | None = None
        for attempt in range(1, max_retries + 2):
            started = time.monotonic()
            try:
                response = requests.post(
                    f"{self.base_url}/api/chat", json=payload, timeout=self.timeout
                )
                response.raise_for_status()
                body = response.json()
                content = body.get("message", {}).get("content", "")
                logger.info(
                    "llm %.0fs (load %.0fs, %d prompt, %d generated tokens)",
                    time.monotonic() - started,
                    body.get("load_duration", 0) / 1e9,
                    body.get("prompt_eval_count", 0),
                    body.get("eval_count", 0),
                )
                if content.strip():
                    return content.strip()
                last_error = LocalModelError("model returned an empty response")
            except requests.RequestException as exc:
                last_error = exc
            logger.warning("chat attempt %d failed: %s", attempt, last_error)
            time.sleep(2 * attempt)

        raise LocalModelError(f"Local model call failed: {last_error}")

    def chat_json(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.2,
        num_predict: int | None = None,
    ) -> dict:
        raw = self.chat(
            system,
            user,
            temperature=temperature,
            json_mode=True,
            num_predict=num_predict,
        )
        parsed = extract_json(raw)
        if parsed is None:
            raise LocalModelError(f"Model did not return valid JSON: {raw[:300]!r}")
        return parsed


def extract_json(text: str) -> dict | None:
    """Small models wrap JSON in prose or fences, and run out of tokens mid-array.

    Anything recoverable is recovered: a reply cut off after ten good facts is
    worth far more than a retry that costs another minute of CPU.
    """
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    while start != -1:
        depth = 0
        for index in range(start, len(text)):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start : index + 1])
                        if isinstance(value, dict):
                            return value
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)

    return _repair_truncated(text)


def _repair_truncated(text: str) -> dict | None:
    """Close an object that was cut off mid-way through generation."""
    start = text.find("{")
    if start == -1:
        return None
    body = text[start:]

    # Prefer cutting back to the last *complete* element, so a half-written
    # final fact is discarded rather than stored as though it were whole.
    for cut in _element_boundaries(body):
        recovered = _close(body[:cut], ("]}", "}"))
        if recovered is not None:
            logger.info("recovered truncated JSON (%d chars kept)", cut)
            return recovered

    # Nothing had a clean boundary, so this is one long string cut mid-sentence
    # (a section of prose). Keeping it is better than discarding the call.
    recovered = _close(body, ('"]}', '"}'))
    if recovered is not None:
        logger.info("recovered truncated JSON string (%d chars)", len(body))
    return recovered


def _close(candidate: str, suffixes: tuple[str, ...]) -> dict | None:
    trimmed = candidate.rstrip().rstrip(",")
    for suffix in suffixes:
        try:
            value = json.loads(trimmed + suffix)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value:
            return value
    return None


def _element_boundaries(body: str) -> list[int]:
    """Offsets just after each complete string element, longest prefix first."""
    points = {match.end() for match in re.finditer(r'"\s*[,\]]', body)}
    return sorted(points, reverse=True)
