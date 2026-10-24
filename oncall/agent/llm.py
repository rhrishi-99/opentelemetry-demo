"""Thin LLM client with tool calling. Messages use the OpenAI chat format.

Only the Groq provider is implemented (OpenAI-compatible chat completions).
Another provider would translate to and from this format inside this module.
"""

import json
import logging
import re
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from oncall.config import Settings, load_settings, secret

PROVIDER_BASE_URLS = {"groq": "https://api.groq.com/openai/v1"}
DEFAULT_MODELS = {"groq": "openai/gpt-oss-120b"}
TIMEOUT_S = 30
# Most a single chat() may spend sleeping for pacing or rate limits.
MAX_WAIT_PER_CALL_S = 45.0
RETRY_STATUSES = (429, 500, 502, 503, 504)
MAX_ATTEMPTS = 4
MAX_WAIT_S = 60
# Per-minute 429s: sleeps allowed per call once every key is cooling down.
MAX_RATE_RETRIES = 4
RATE_LIMIT_DEFAULT_S = 5.0
RATE_LIMIT_MARGIN_S = 0.5
# Pace each key below Groq's 8000 tokens/minute free-tier limit.
TOKENS_PER_MINUTE = 7000
WINDOW_S = 60.0
# Extra keys for the primary model, tried in order after LLM_API_KEY.
EXTRA_KEY_ENVS = tuple(f"GROQ_KEY_{n}" for n in range(2, 10))
BAD_GENERATION_CODES = ("tool_use_failed", "output_parse_failed")
# Matches the wait in Groq's rate-limit message: "try again in 18.77s", "in 590ms"
# or "in 1m2.5s" (group 1 = minutes, 2 = number, 3 = unit).
RETRY_IN = re.compile(r"try again in (?:(\d+)m)?([0-9.]+)(ms|s)")
# A daily quota in a rate-limit message: "tokens per day (TPD)", "requests per
# day (RPD)" or any "per day"/"daily" wording, in any case.
DAILY_QUOTA = re.compile(r"per day|\b(TPD|RPD)\b|daily", re.IGNORECASE)
# Groq is behind Cloudflare, which rejects the default Python-urllib agent (error 1010).
USER_AGENT = "incident-zero-oncall/0.1"

# transport(url, headers, body) -> (status, response_json_text)
Transport = Callable[[str, dict, dict], tuple[int, str]]

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """The LLM call failed or returned something unusable."""

    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass
class LLMReply:
    text: str
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    model: str = ""
    key: str = ""


def _urllib_transport(url: str, headers: dict, body: dict) -> tuple[int, str]:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LLMError(f"request to {url} failed: {exc}") from exc


def to_function_tools(schemas: list[dict]) -> list[dict]:
    """Convert registry schemas ({name, description, input_schema}) to function tools."""
    return [
        {
            "type": "function",
            "function": {
                "name": s["name"],
                "description": s["description"],
                "parameters": s["input_schema"],
            },
        }
        for s in schemas
    ]


def _retryable(status: int, text: str) -> bool:
    """Rate limits, server errors, and Groq 400s caused by a malformed generation."""
    return status in RETRY_STATUSES or (
        status == 400 and any(code in text for code in BAD_GENERATION_CODES)
    )


def _hinted_s(text: str) -> float | None:
    match = RETRY_IN.search(text)
    if not match:
        return None
    minutes, number, unit = match.groups()
    seconds = float(number) / 1000 if unit == "ms" else float(number)
    return int(minutes or 0) * 60 + seconds


def _wait_s(text: str, attempt: int) -> float:
    """Seconds to wait before retrying a server error: the hint if given, else backoff."""
    hinted = _hinted_s(text)
    return min(hinted if hinted is not None else 2**attempt, MAX_WAIT_S)


def rate_limit_wait_s(text: str) -> float:
    """Seconds to wait after a per-minute 429: Groq's hint (default 5 s) plus a margin."""
    hinted = _hinted_s(text)
    wait = RATE_LIMIT_DEFAULT_S if hinted is None else hinted
    return min(wait + RATE_LIMIT_MARGIN_S, MAX_WAIT_S)


def _parse_tool_call(raw: dict) -> dict:
    fn = raw.get("function") or {}
    call = {"id": raw.get("id", ""), "name": fn.get("name", ""), "args": None}
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except json.JSONDecodeError as exc:
        call["error"] = f"arguments are not valid JSON: {exc}"
        return call
    if not isinstance(args, dict):
        call["error"] = "arguments must be a JSON object"
        return call
    call["args"] = args
    return call


@dataclass(frozen=True)
class Endpoint:
    """One entry of the fallback chain."""

    provider: str
    model: str
    key_env: str

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"

    @property
    def budget(self) -> str:
        """Groq rate-limits each key per model."""
        return f"{self.key_env}:{self.model}"


# Entries whose daily quota ran out; skipped for the rest of the process.
EXHAUSTED: set[Endpoint] = set()
# key env name -> (monotonic time, tokens) of its calls in the last WINDOW_S.
USAGE: dict[str, deque] = {}


def estimate_tokens(body: dict) -> int:
    """Tokens Groq counts for a request: the prompt (~4 chars/token) plus max output."""
    prompt = json.dumps(body.get("messages", [])) + json.dumps(body.get("tools", []))
    return len(prompt) // 4 + int(body.get("max_completion_tokens") or 0)


def pacing_wait_s(key_env: str, tokens: int, now: float) -> float:
    """Seconds until `tokens` more fit in the key's per-minute budget (0 if now)."""
    window = USAGE.setdefault(key_env, deque())
    while window and now - window[0][0] >= WINDOW_S:
        window.popleft()
    used = sum(spent for _, spent in window)
    if used + tokens <= TOKENS_PER_MINUTE:
        return 0.0
    for at, spent in window:
        used -= spent
        if used + tokens <= TOKENS_PER_MINUTE:
            return max(0.0, at + WINDOW_S - now)
    # A request bigger than the whole budget waits for an empty window.
    return max(0.0, window[-1][0] + WINDOW_S - now)


def record_usage(key_env: str, tokens: int, now: float) -> None:
    USAGE.setdefault(key_env, deque()).append((now, tokens))


def parse_fallbacks(spec: str) -> list[Endpoint]:
    """ "groq:openai/gpt-oss-120b:GROQ_KEY_1,..." -> Endpoints (model may hold ':')."""
    endpoints = []
    for item in (part.strip() for part in spec.split(",")):
        if not item:
            continue
        provider, _, rest = item.partition(":")
        model, _, key_env = rest.rpartition(":")
        if not (provider and model and key_env):
            raise LLMError(
                f"bad LLM_FALLBACKS entry {item!r}; use provider:model:KEY_ENV_NAME"
            )
        endpoints.append(Endpoint(provider.lower(), model, key_env))
    return endpoints


def is_daily_quota(status: int, text: str) -> bool:
    """A 429 that will not clear by waiting minutes (Groq: tokens/requests per day)."""
    return status == 429 and bool(DAILY_QUOTA.search(text))


class QuotaExhausted(LLMError):
    """The endpoint's daily quota is used up; try the next one."""


class RateLimited(LLMError):
    """A per-minute 429: try another key now, or wait."""


class LLM:
    def __init__(
        self, settings: Settings | None = None, transport: Transport | None = None
    ):
        self.settings = settings or load_settings()
        self.transport = transport or _urllib_transport
        if self.settings.LLM_FALLBACKS:
            self.chain = parse_fallbacks(self.settings.LLM_FALLBACKS)
            self.keys = {e.key_env: secret(e.key_env) for e in self.chain}
        else:
            provider = (self.settings.LLM_PROVIDER or "").lower()
            model = self.settings.LLM_MODEL or DEFAULT_MODELS.get(provider, "")
            self.chain = [Endpoint(provider, model, "LLM_API_KEY")]
            self.keys = {"LLM_API_KEY": self.settings.LLM_API_KEY}
            for key_env in EXTRA_KEY_ENVS:
                if key := secret(key_env):
                    self.chain.append(Endpoint(provider, model, key_env))
                    self.keys[key_env] = key
        for e in self.chain:
            if e.provider not in PROVIDER_BASE_URLS:
                raise LLMError(
                    f"unsupported LLM_PROVIDER {e.provider!r}; "
                    f"supported: {', '.join(PROVIDER_BASE_URLS)}"
                )
            if not self.keys.get(e.key_env):
                raise LLMError(f"{e.key_env} is not set (put it in oncall/.env)")
        first = self.chain[0]
        self.provider, self.model = first.provider, first.model
        # endpoint -> monotonic time its per-minute 429 clears
        self.cooldown: dict[Endpoint, float] = {}

    def _url(self, endpoint: Endpoint) -> str:
        base = self.settings.LLM_BASE_URL or PROVIDER_BASE_URLS[endpoint.provider]
        return base.rstrip("/") + "/chat/completions"

    def chat(
        self, messages: list[dict], tools: list[dict], tool_choice: str = "required"
    ) -> LLMReply:
        """Send one chat turn at temperature 0; by default the model must call a tool.

        Every call goes through here. It picks the first endpoint (key) that is
        free now: not out of daily quota (skipped for the rest of the process),
        not cooling down after a per-minute 429, and with room in its token
        budget. If none is free it waits for the soonest one. reply.model and
        reply.key name the endpoint that served the call.
        """
        skipped: list[str] = []
        rate_waits = 0
        waited = 0.0
        last_429 = ""
        while True:
            live = [e for e in self.chain if e not in EXHAUSTED]
            if not live:
                raise LLMError(
                    "all LLM endpoints are out of daily quota: "
                    f"{', '.join(skipped or [e.label for e in self.chain])}",
                    status=429,
                )
            now = time.monotonic()
            tokens = estimate_tokens(self._body(live[0], messages, tools, tool_choice))
            wait, _, endpoint = min(
                (
                    max(
                        self.cooldown.get(e, 0.0) - now,
                        pacing_wait_s(e.budget, tokens, now),
                    ),
                    i,
                    e,
                )
                for i, e in enumerate(live)
            )
            if wait > 0:
                if waited + wait > MAX_WAIT_PER_CALL_S:
                    raise LLMError(
                        f"LLM wait budget exceeded: would wait {waited + wait:.0f}s "
                        f"(limit {MAX_WAIT_PER_CALL_S:.0f}s) for {endpoint.budget}",
                        status=429,
                        body=last_429,
                    )
                waited += wait
                if self.cooldown.get(endpoint, 0.0) > now:
                    if rate_waits == MAX_RATE_RETRIES:
                        raise LLMError(
                            f"rate limited on every key after {MAX_RATE_RETRIES} "
                            f"retries: {last_429[:300]}",
                            status=429,
                            body=last_429,
                        )
                    rate_waits += 1
                    log.info("rate limited on every key: waiting %.1fs", wait)
                else:
                    log.info("pacing %.1fs on %s", wait, endpoint.budget)
                time.sleep(wait)
                self.cooldown.pop(endpoint, None)
            try:
                return self._chat_one(endpoint, messages, tools, tokens, tool_choice)
            except QuotaExhausted:
                EXHAUSTED.add(endpoint)
                skipped.append(endpoint.label)
            except RateLimited as exc:
                last_429 = exc.body
                self.cooldown[endpoint] = time.monotonic() + rate_limit_wait_s(exc.body)
                log.info("429 on %s: trying the next key", endpoint.budget)

    def _body(
        self,
        endpoint: Endpoint,
        messages: list[dict],
        tools: list[dict],
        tool_choice: str = "required",
    ) -> dict:
        body = {
            "model": endpoint.model,
            "messages": messages,
            "temperature": 0,
            "max_completion_tokens": self.settings.LLM_MAX_TOKENS,
        }
        if tools:
            body["tools"] = to_function_tools(tools)
            body["tool_choice"] = tool_choice
        return body

    def _chat_one(
        self,
        endpoint: Endpoint,
        messages: list[dict],
        tools: list[dict],
        tokens: int,
        tool_choice: str = "required",
    ) -> LLMReply:
        body = self._body(endpoint, messages, tools, tool_choice)
        headers = {
            "Authorization": f"Bearer {self.keys[endpoint.key_env]}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        url = self._url(endpoint)
        status, text = 0, ""
        for attempt in range(MAX_ATTEMPTS):
            try:
                status, text = self.transport(url, headers, body)
            except LLMError:
                # Network errors (DNS, refused, timeout) get the same backoff as 5xx.
                if attempt == MAX_ATTEMPTS - 1:
                    raise
                time.sleep(_wait_s("", attempt))
                continue
            if is_daily_quota(status, text):
                raise QuotaExhausted(
                    f"{endpoint.label} daily quota exhausted: {text[:300]}",
                    status=status,
                    body=text,
                )
            if status == 429:
                raise RateLimited(
                    f"{endpoint.label} rate limited: {text[:300]}",
                    status=status,
                    body=text,
                )
            if not _retryable(status, text) or attempt == MAX_ATTEMPTS - 1:
                break
            time.sleep(_wait_s(text, attempt))
        if status != 200:
            raise LLMError(
                f"{endpoint.provider} returned HTTP {status}: {text[:500]}",
                status=status,
                body=text,
            )
        try:
            data = json.loads(text)
            message = data["choices"][0]["message"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise LLMError(
                f"unexpected {endpoint.provider} response: {text[:500]}"
            ) from exc
        usage = data.get("usage") or {}
        record_usage(
            endpoint.budget, int(usage.get("total_tokens") or tokens), time.monotonic()
        )
        log.info("LLM call served by %s with %s", endpoint.label, endpoint.key_env)
        return LLMReply(
            text=message.get("content") or "",
            tool_calls=[_parse_tool_call(c) for c in message.get("tool_calls") or []],
            usage=usage,
            model=endpoint.label,
            key=endpoint.key_env,
        )
