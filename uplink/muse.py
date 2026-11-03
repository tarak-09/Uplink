"""Meta Model API: Muse Spark for writing, Muse Voice Transcribe for listening."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .transport import Part, Transport, TransportError, encode_multipart


class ModelAPIError(Exception):
    """The Model API could not produce an answer."""


@dataclass(frozen=True)
class Transcript:
    text: str
    duration_ms: int | None = None
    session_id: str | None = None


class MetaModelAPI:
    def __init__(
        self,
        *,
        api_key: str,
        transport: Transport,
        base_url: str = "https://api.meta.ai/v1",
        model: str = "muse-spark-1.3",
        asr_model: str = "muse-voice-transcribe-1.0",
        reasoning_effort: str = "low",
        # A cap, not a target. Reasoning counts toward it: at "low" Muse Spark spent 827-952 tokens
        # reasoning about one alert, so a 1024 cap cut alerts off (Telugu often before any text).
        max_completion_tokens: int = 4096,
    ) -> None:
        if not api_key:
            raise ValueError("an API key is required")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._transport = transport
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.asr_model = asr_model
        self.reasoning_effort = reasoning_effort
        self.max_completion_tokens = max_completion_tokens

    def chat(self, messages: Sequence[Mapping[str, str]]) -> str:
        """Return the model's reply to a conversation of developer/user/assistant messages."""
        payload = {
            "model": self.model,
            "messages": list(messages),
            "max_completion_tokens": self.max_completion_tokens,
            "reasoning_effort": self.reasoning_effort,
        }
        try:
            data = self._transport.json(
                "POST", f"{self.base_url}/chat/completions", payload, headers=self._headers, timeout=60, idempotent=True
            )
        except TransportError as exc:
            raise ModelAPIError(_explain(exc)) from None
        return _reply_text(data, self.max_completion_tokens)

    def transcribe(self, wav: bytes, *, languages: Sequence[str] = (), keywords: Sequence[str] = ()) -> Transcript:
        """Transcribe a mono 16-bit WAV (16 or 24 kHz)."""
        request: dict[str, Any] = {"model": self.asr_model, "audioEncoding": "WAV", "mode": "PUSH_TO_TALK"}
        if languages:
            request["languageBias"] = list(languages)
        if keywords:
            request["keywords"] = list(keywords)
        body, content_type = encode_multipart(
            [
                Part("request", json.dumps(request).encode("utf-8"), "application/json"),
                Part("audio", wav, "audio/wav", filename="voice.wav"),
            ]
        )
        headers = {**self._headers, "Content-Type": content_type, "Accept": "application/json"}
        try:
            response = self._transport.request(
                "POST", f"{self.base_url}/asr/transcribe", headers=headers, body=body, timeout=60, idempotent=True
            )
            data = response.json()
        except TransportError as exc:
            raise ModelAPIError(_explain(exc)) from None
        text = data.get("transcript") if isinstance(data, dict) else None
        if not isinstance(text, str):
            raise ModelAPIError("the transcription had no transcript")
        duration = data.get("audioDurationMs")
        return Transcript(
            text=text.strip(),
            duration_ms=int(duration) if isinstance(duration, (int, float)) else None,
            session_id=data.get("sessionId"),
        )


def _reply_text(data: Any, limit: int) -> str:
    try:
        choice = data["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise ModelAPIError("the reply had no choices[0].message.content") from None
    if choice.get("finish_reason") == "length":  # half an alert can still pass the draft check
        raise ModelAPIError(f"the reply was cut off at the {limit}-token limit (reasoning counts toward it)")
    if isinstance(content, list):  # some OpenAI-compatible servers return content parts
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not isinstance(content, str):
        raise ModelAPIError("the reply content was not text")
    return content


def _explain(error: TransportError) -> str:
    detail = error.json()
    if isinstance(detail, dict):
        inner = detail.get("error", detail)
        message = inner.get("message") if isinstance(inner, dict) else inner
        if message:
            return f"{error} ({message})"
    return str(error)
