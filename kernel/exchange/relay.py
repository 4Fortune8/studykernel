"""Optional automatic delivery of the briefing. DESIGN.md §16 v2.

The exchange protocol is copy/paste first and that does not change: DESIGN.md
§10 is titled "no-API" because the system has to work without one, and every
guarantee that makes the protocol safe lives on the *inbound* side --
`record.parse` rejects on `item_id` mismatch whether the reply was typed back
from a chat window or arrived over a socket. This module only removes the
typing.

Three properties it must have, in order:

1. **Optional.** `configured()` is false with no key set, and every front end
   falls back to the paste box. A failed relay is never a failed drill: the
   attempt is already durable by the time a briefing exists at all.
2. **Dependency-free.** The kernel is stdlib plus a YAML parser (pyproject),
   and one JSON POST does not justify changing that. `urllib.request` is
   sufficient and the request body is thirty lines of dict.
3. **Structured or nothing.** The reply comes back under a response schema, so
   the transport cannot return prose, an unfenced block, or an invented error
   code. It still goes through `record.parse` -- the schema makes the common
   failures unrepresentable, it does not make the payload trusted.

Configuration is environment, read once per call rather than cached, so
rotating a key does not need a restart. `.env` is loaded by whichever front end
starts up; see `kernel.config.load_env_file`.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

# The key may be named for the console it was minted in or for the role it
# plays here; both are read, in this order, so an existing `.env` works
# untouched and a deployment that would rather be explicit can be.
KEY_VARS = ("STUDY_RELAY_KEY", "aistudioAPI", "GEMINI_API_KEY", "GOOGLE_API_KEY")

# A reasoning-capable fast model. The task is short, structured and latency
# sensitive -- it sits between a learner and the diagnosis they are waiting
# for -- and the hard part is reading someone's rationale carefully, not
# breadth. Override with STUDY_RELAY_MODEL when that trade changes.
DEFAULT_MODEL = "gemini-3.5-flash"
DEFAULT_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta"
# Long enough for a thinking model on a long passage, short enough that a
# hung request does not hold the drill panel open indefinitely.
DEFAULT_TIMEOUT = 120.0


class RelayError(RuntimeError):
    """The relay could not produce a reply. Worded for the learner.

    Every message this raises is shown on the page above a paste box, so it has
    to say what happened *and* leave the copy/paste path visibly intact.
    """


class NotConfigured(RelayError):
    """No API key. Not an error condition -- the default state of the system."""


@dataclass(frozen=True)
class RelayConfig:
    api_key: str
    model: str = DEFAULT_MODEL
    endpoint: str = DEFAULT_ENDPOINT
    timeout: float = DEFAULT_TIMEOUT

    @property
    def url(self) -> str:
        return f"{self.endpoint}/models/{self.model}:generateContent"


@dataclass(frozen=True)
class RelayReply:
    """What came back, and who said it."""

    text: str
    model: str
    # Recorded on the exchange, so a later reader can tell a diagnosis a model
    # produced from one a human typed back. See `storage/schema.sql`.
    responder: str


def api_key() -> str | None:
    for var in KEY_VARS:
        value = (os.environ.get(var) or "").strip()
        if value:
            return value
    return None


def configured() -> bool:
    """Whether auto-send is available at all. Front ends branch on this."""
    return api_key() is not None


def from_env() -> RelayConfig:
    key = api_key()
    if key is None:
        raise NotConfigured(
            "no API key set, so the briefing cannot be sent automatically. "
            f"Set one of {', '.join(KEY_VARS)} in .env, or paste the briefing "
            "into a chat client -- the loop works either way."
        )
    return RelayConfig(
        api_key=key,
        model=os.environ.get("STUDY_RELAY_MODEL") or DEFAULT_MODEL,
        endpoint=os.environ.get("STUDY_RELAY_ENDPOINT") or DEFAULT_ENDPOINT,
        timeout=float(os.environ.get("STUDY_RELAY_TIMEOUT") or DEFAULT_TIMEOUT),
    )


def build_request(
    body: str,
    system: str | None = None,
    schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The request payload. Split out so a test can read it without a socket.

    Deliberately minimal. No temperature, no thinking budget, no candidate
    count: every generation knob is model-specific, the defaults are what the
    model was tuned against, and a knob set here for one model is a silent
    regression the day `STUDY_RELAY_MODEL` changes.
    """
    payload: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": body}]}],
    }
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    if schema is not None:
        payload["generationConfig"] = {
            "responseMimeType": "application/json",
            "responseSchema": schema,
        }
    return payload


def send(
    body: str,
    system: str | None = None,
    schema: dict[str, Any] | None = None,
    config: RelayConfig | None = None,
) -> RelayReply:
    """Send one briefing and return the reply text, unparsed.

    Unparsed on purpose: `record.parse` is the only thing allowed to decide
    whether a payload is usable, and it does that identically for both halves
    of the protocol. A transport that started interpreting replies would be a
    second, quieter validator.
    """
    cfg = config or from_env()
    payload = build_request(body, system, schema)
    request = urllib.request.Request(
        cfg.url,
        data=json.dumps(payload).encode(),
        headers={
            "content-type": "application/json",
            "x-goog-api-key": cfg.api_key,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=cfg.timeout) as response:
            raw = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise RelayError(_http_message(exc, cfg)) from exc
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        raise RelayError(
            f"could not reach the tutoring API ({exc}). Paste the briefing into a "
            "chat client instead -- nothing has been lost."
        ) from exc
    except json.JSONDecodeError as exc:
        raise RelayError(f"the tutoring API returned something that is not JSON: {exc}") from exc

    return RelayReply(text=extract_text(raw), model=cfg.model, responder=f"api:{cfg.model}")


def extract_text(raw: dict[str, Any]) -> str:
    """Pull the reply text out of a generateContent response.

    Thought parts are dropped. A reasoning model returns its scratchpad in the
    same `parts` list flagged `thought`, and concatenating it into the payload
    turns a valid structured reply into a JSON parse error -- with the model
    blamed for something the transport did.
    """
    if "error" in raw:
        detail = raw["error"].get("message", raw["error"])
        raise RelayError(f"the tutoring API refused the request: {detail}")

    blocked = (raw.get("promptFeedback") or {}).get("blockReason")
    if blocked:
        raise RelayError(
            f"the tutoring API blocked the briefing ({blocked}). Paste it into a "
            "chat client instead."
        )

    candidates = raw.get("candidates") or []
    if not candidates:
        raise RelayError("the tutoring API returned no reply at all")

    candidate = candidates[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(
        part.get("text", "") for part in parts if not part.get("thought")
    ).strip()

    if not text:
        reason = candidate.get("finishReason") or "unknown"
        if reason == "MAX_TOKENS":
            raise RelayError(
                "the tutoring reply was cut off before any of it arrived -- the "
                "item is probably too long for the configured model."
            )
        raise RelayError(f"the tutoring API returned an empty reply (finishReason {reason})")

    return text


def _http_message(exc: urllib.error.HTTPError, cfg: RelayConfig) -> str:
    """Turn a status code into something a learner can do something about.

    A raw 429 on the page above a paste box tells someone their study session
    is broken. What it actually means is "use the other path for a bit", and
    only this function knows that.
    """
    try:
        detail = json.loads(exc.read().decode()).get("error", {}).get("message", "")
    except Exception:  # noqa: BLE001 -- the body is best-effort context, never the point
        detail = ""

    if exc.code in (401, 403):
        return (
            "the tutoring API rejected the key. Check the value in .env "
            f"({' / '.join(KEY_VARS)}) and that it is enabled for this project. {detail}"
        ).strip()
    if exc.code == 404:
        return (
            f"the tutoring API has no model called {cfg.model!r}. Set "
            f"STUDY_RELAY_MODEL to one your key can reach. {detail}"
        ).strip()
    if exc.code == 429:
        return (
            "the tutoring API is rate limiting or out of quota. Paste the briefing "
            "into a chat client for now, or come back to it from history."
        )
    if exc.code >= 500:
        return (
            f"the tutoring API is unavailable right now (HTTP {exc.code}). The "
            "briefing is stored -- history can pick this exchange back up."
        )
    return f"the tutoring API returned HTTP {exc.code}. {detail}".strip()
