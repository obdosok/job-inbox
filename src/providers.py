"""One schema-constrained call, two providers.

Extraction and assessment both want the same thing: send a system prompt and a
job posting, get back JSON that matches a schema. Only the wire format differs,
so that difference lives here and nowhere else.

Raw ``urllib`` on purpose. The project runs on a stock Python with no installed
packages, and both official SDKs would break that.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Protocol


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"

OPENAI_DEFAULT_MODEL = "gpt-5-mini"
ANTHROPIC_DEFAULT_MODEL = "claude-opus-5"
ANTHROPIC_VERSION = "2023-06-01"

# Anthropic's structured outputs reject these; the schemas here use them for
# documentation and `validate_*` re-checks every one of them client-side anyway.
UNSUPPORTED_BY_ANTHROPIC = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
                            "multipleOf", "minLength", "maxLength", "minItems", "maxItems", "pattern")


def load_env_file(path: Path = ENV_FILE) -> list[str]:
    """Read ``.env`` from the project root into the environment.

    A real environment variable always wins, so an exported key overrides the
    file and nothing here can silently replace a deliberate setting. Returns the
    names it filled in, for `profile show` to report.
    """
    if not path.is_file():
        return []
    filled = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip().strip("'\"")
        # Presence wins, not truthiness: `ANTHROPIC_API_KEY=` in the shell means
        # "deliberately off" and the file must not quietly switch it back on.
        if name and value and name not in os.environ:
            os.environ[name] = value
            filled.append(name)
    return filled


load_env_file()


class Provider(Protocol):
    name: str
    model: str

    def complete_json(self, system: str, user: str, schema: dict[str, Any], schema_name: str) -> tuple[dict[str, Any], dict[str, Any]]: ...


def _post(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        # Both APIs explain themselves in the response body. Without it a caller
        # sees a bare "404" and cannot tell a wrong model from a wrong endpoint.
        detail = ""
        try:
            detail = error.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        raise ValueError(f"{url} returned HTTP {error.code}: {detail or error.reason}") from error


def normalize_base_url(url: str) -> str:
    """Accept a base URL with or without the version segment.

    Both conventions are in the wild: SDKs are usually given the bare origin and
    append the version themselves, while raw-HTTP setups embed ``/v1``. Guessing
    wrong yields a bare 404 that looks like a bad endpoint.
    """
    url = url.rstrip("/")
    return url if re.search(r"/v\d+$", url) else f"{url}/v1"


def _without(schema: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(schema, dict):
        return {key: _without(value, keys) for key, value in schema.items() if key not in keys}
    if isinstance(schema, list):
        return [_without(item, keys) for item in schema]
    return schema


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str, model: str, base_url: str = "https://api.openai.com/v1", timeout: int = 90):
        self.api_key = api_key
        self.model = model
        self.base_url = normalize_base_url(base_url)
        self.timeout = timeout

    def complete_json(self, system: str, user: str, schema: dict[str, Any], schema_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
        body = _post(
            f"{self.base_url}/responses",
            {"Authorization": f"Bearer {self.api_key}"},
            {
                "model": self.model, "store": False, "instructions": system, "input": user,
                "text": {"format": {"type": "json_schema", "name": schema_name, "strict": True, "schema": schema}},
            },
            self.timeout,
        )
        output_text = body.get("output_text")
        if not output_text:
            chunks = []
            for item in body.get("output", []):
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        chunks.append(content.get("text", ""))
            output_text = "".join(chunks)
        if not output_text:
            raise ValueError("The model returned no output text")
        usage = body.get("usage") or {}
        return json.loads(output_text), {
            "provider": self.name, "model": self.model,
            "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
            "cached_input_tokens": (usage.get("input_tokens_details") or {}).get("cached_tokens"),
        }


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, model: str, base_url: str = "https://api.anthropic.com/v1",
                 timeout: int = 180, max_tokens: int = 16000, effort: str = "", cache_ttl: str = "1h"):
        self.api_key = api_key
        self.model = model
        self.base_url = normalize_base_url(base_url)
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.effort = effort
        # The default five-minute cache suits a batch run and nothing else: a
        # posting pasted twenty minutes after the last one re-pays for the whole
        # 18k-token profile. An hour matches how the inbox is actually used.
        self.cache_ttl = cache_ttl

    def complete_json(self, system: str, user: str, schema: dict[str, Any], schema_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": _without(schema, UNSUPPORTED_BY_ANTHROPIC)}}
        if self.effort:
            output_config["effort"] = self.effort
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            # The system prefix is byte-identical on every call and dwarfs the
            # posting, so it is the cache breakpoint and the posting stays in
            # messages where it belongs.
            "system": [{"type": "text", "text": system,
                        "cache_control": {"type": "ephemeral", **({"ttl": self.cache_ttl} if self.cache_ttl else {})}}],
            "messages": [{"role": "user", "content": user}],
            "output_config": output_config,
        }
        body = _post(f"{self.base_url}/messages",
                     {"x-api-key": self.api_key, "anthropic-version": ANTHROPIC_VERSION},
                     payload, self.timeout)
        stop = body.get("stop_reason")
        if stop == "refusal":
            details = body.get("stop_details") or {}
            raise ValueError(f"The model declined this request ({details.get('category') or 'unspecified'})")
        if stop == "max_tokens":
            raise ValueError("The response hit max_tokens and is incomplete; raise JOB_INBOX_MAX_TOKENS")
        text = next((block.get("text", "") for block in body.get("content", []) if block.get("type") == "text"), "")
        if not text:
            raise ValueError("The model returned no text block")
        usage = body.get("usage") or {}
        return json.loads(text), {
            "provider": self.name, "model": self.model,
            "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
            "cached_input_tokens": usage.get("cache_read_input_tokens"),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        }


def configured_provider() -> str:
    """Which provider to use: an explicit choice, else whichever key exists."""
    chosen = os.environ.get("JOB_INBOX_PROVIDER", "").strip().lower()
    if chosen in {"anthropic", "openai"}:
        return chosen
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return ""


def has_key(provider: str = "") -> bool:
    provider = provider or configured_provider()
    if provider == "anthropic":
        return bool(os.environ.get("ANTHROPIC_API_KEY"))
    if provider == "openai":
        return bool(os.environ.get("OPENAI_API_KEY"))
    return False


def default_model(provider: str = "") -> str:
    provider = provider or configured_provider()
    return ANTHROPIC_DEFAULT_MODEL if provider == "anthropic" else OPENAI_DEFAULT_MODEL


def build(model_override: str = "", timeout: int = 90) -> Provider | None:
    """The configured provider, or ``None`` when no key is set."""
    provider = configured_provider()
    if not has_key(provider):
        return None
    model = model_override or os.environ.get("JOB_INBOX_LLM_MODEL") or default_model(provider)
    if provider == "anthropic":
        return AnthropicProvider(
            os.environ["ANTHROPIC_API_KEY"], model,
            os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1"),
            timeout=max(timeout, 180),
            max_tokens=int(os.environ.get("JOB_INBOX_MAX_TOKENS", "16000")),
            effort=os.environ.get("JOB_INBOX_EFFORT", ""),
            cache_ttl=os.environ.get("JOB_INBOX_CACHE_TTL", "1h"),
        )
    return OpenAIProvider(os.environ["OPENAI_API_KEY"], model,
                          os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"), timeout=timeout)
