"""Speech-to-text proxy for the PWA's hold-to-talk voice input.

The DashScope API key never reaches the browser: the client uploads a short
recording to an authenticated ``POST /api/v1/asr`` endpoint and this module
forwards it to the Qwen-Audio-ASR (synchronous, non-realtime) model.

Environment variables:

- ``DASHSCOPE_API_KEY`` (required to enable ASR)
- ``OTTY_ASR_MODEL`` (default ``qwen-audio-3.1-asr-flash``)
- ``OTTY_ASR_ENDPOINT`` (default DashScope Beijing multimodal-generation URL;
  use ``https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation``
  for a workspace-specific domain)
"""

from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator

DEFAULT_ASR_MODEL = "qwen-audio-3.1-asr-flash"
DEFAULT_ASR_ENDPOINT = (
    "https://llm-6b7p48qstpbrqpqn.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
)
# ~60 s of 16 kHz / 16-bit mono WAV is ~1.9 MB; leave headroom for other formats.
MAX_AUDIO_BYTES = 8 * 1024 * 1024
MAX_AUDIO_B64_CHARS = (MAX_AUDIO_BYTES * 4) // 3 + 4

AUDIO_MIME = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "ogg": "audio/ogg",
    "webm": "audio/webm",
}


@dataclass(frozen=True)
class AsrSettings:
    api_key: str = ""
    model: str = DEFAULT_ASR_MODEL
    endpoint: str = DEFAULT_ASR_ENDPOINT
    timeout: float = 45.0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key.strip())

    @classmethod
    def from_env(cls) -> AsrSettings:
        return cls(
            api_key=(os.getenv("DASHSCOPE_API_KEY") or "").strip(),
            model=(os.getenv("OTTY_ASR_MODEL") or DEFAULT_ASR_MODEL).strip(),
            endpoint=(os.getenv("OTTY_ASR_ENDPOINT") or DEFAULT_ASR_ENDPOINT).strip(),
        )


class AsrRequest(BaseModel):
    audio: str = Field(
        min_length=16,
        max_length=MAX_AUDIO_B64_CHARS,
        description="Base64-encoded audio bytes (no data: prefix)",
    )
    format: Literal["wav", "mp3", "m4a", "aac", "ogg", "webm"] = "wav"
    sample_rate: int = Field(default=16000, ge=8000, le=48000)

    @field_validator("audio")
    @classmethod
    def valid_base64(cls, value: str) -> str:
        if value.startswith("data:"):
            value = value.split(",", 1)[-1]
        try:
            raw = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("audio must be valid base64") from exc
        if not raw or len(raw) > MAX_AUDIO_BYTES:
            raise ValueError("audio is empty or too large")
        return value


class AsrResult(BaseModel):
    text: str
    model: str


def extract_text(payload: Any) -> str:
    """Read the transcript from a Qwen-Audio-ASR response (several shapes)."""
    if not isinstance(payload, dict):
        return ""
    output = payload.get("output") or {}
    if isinstance(output, dict):
        if isinstance(output.get("text"), str):
            return output["text"]
        sentence = output.get("sentence")
        if isinstance(sentence, dict) and isinstance(sentence.get("text"), str):
            return sentence["text"]
        choices = output.get("choices")
        if isinstance(choices, list) and choices:
            content = (choices[0].get("message") or {}).get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return "".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
    return ""


def build_payload(settings: AsrSettings, body: AsrRequest) -> dict[str, Any]:
    mime = AUDIO_MIME.get(body.format, "audio/wav")
    return {
        "model": settings.model,
        "input": {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {"data": f"data:{mime};base64,{body.audio}"},
                        }
                    ],
                }
            ]
        },
        "parameters": {"format": body.format, "sample_rate": str(body.sample_rate)},
    }


async def transcribe(
    settings: AsrSettings,
    body: AsrRequest,
    client: httpx.AsyncClient | None = None,
) -> AsrResult:
    if not settings.enabled:
        raise HTTPException(503, "Voice input is not configured (set DASHSCOPE_API_KEY)")
    headers = {
        "Authorization": f"Bearer {settings.api_key}",
        "Content-Type": "application/json",
        "X-DashScope-SSE": "disable",
    }
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=settings.timeout)
    try:
        res = await client.post(
            settings.endpoint, json=build_payload(settings, body), headers=headers
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"ASR service unreachable: {exc}") from exc
    finally:
        if owns_client:
            await client.aclose()
    try:
        payload = res.json()
    except ValueError:
        payload = {}
    if res.status_code >= 400:
        message = payload.get("message") if isinstance(payload, dict) else None
        raise HTTPException(502, f"ASR failed ({res.status_code}): {message or res.text[:200]}")
    return AsrResult(text=extract_text(payload).strip(), model=settings.model)
