"""
The LLM that answers cache misses. Settings are in the [llm] section of gateway.config.

The Gemini API key comes from the GEMINI_API_KEY environment variable, never from the
config file. With provider = mock, no key is needed and the answer is a fixed template.
"""
import math
import os
from dataclasses import dataclass

from google import genai
from google.genai import errors, types

from smart_ai_gateway.config import get_settings

_cfg = get_settings().llm
_mock_template = get_settings().scheduler.mock_llm_response_template

# HTTP codes worth retrying: Google's servers briefly failing or overloaded. Left out:
# 504 (deadline exceeded: the full timeout is already spent, so retrying multiplies the wait)
# and 429 (rate limit: the free tier's limits are per minute and per day).
_RETRY_STATUS_CODES = [500, 502, 503]


@dataclass
class LLMAnswer:
    """
    An answer plus what the gateway needs to account for it.

    cacheable is True only for a complete answer (finish reason STOP; None is accepted when
    the API gave no finish reason but did give text). Truncated (MAX_TOKENS) or filtered
    (SAFETY, RECITATION, ...) answers are still returned to the user but must not be cached.
    usage holds prompt_tokens, output_tokens, thinking_tokens, total_tokens (ints), model (str)
    and estimated (True when the counts are guessed rather than reported by the API).
    """
    text: str
    cacheable: bool
    finish_reason: "str | None"
    usage: dict


class LLMUnavailable(Exception):
    """
    The LLM gave no usable answer. The request fails and nothing is cached.
    """

    def __init__(self, message: str, rate_limited: bool = False):
        super().__init__(message)
        self.rate_limited = rate_limited


def _create_client():
    if _cfg.provider == "mock":
        return None

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "[llm] provider = gemini needs a Gemini API key in the GEMINI_API_KEY environment "
            "variable (see .env.example), or set provider = mock in gateway.config"
        )
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=_cfg.timeout_seconds * 1000,  # milliseconds
            retry_options=types.HttpRetryOptions(
                attempts=_cfg.max_retries + 1,
                http_status_codes=_RETRY_STATUS_CODES
            )
        )
    )


# Created once at startup, so a missing key stops the gateway straight away
client = _create_client()


def source_name() -> str:
    """
    The "source" reported for answers from this LLM.
    """
    return "mock_llm" if client is None else _cfg.provider


def _generation_config() -> types.GenerateContentConfig:
    thinking = types.ThinkingConfig(thinking_level=_cfg.thinking_level.upper()) if _cfg.thinking_level else None
    return types.GenerateContentConfig(
        system_instruction=_cfg.system_instruction,
        max_output_tokens=_cfg.max_output_tokens,
        thinking_config=thinking
    )


def _estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


def _finish_name(candidate) -> "str | None":
    reason = getattr(candidate, "finish_reason", None) if candidate is not None else None
    if reason is None:
        return None
    name = getattr(reason, "name", None) or str(reason)
    return name.rsplit(".", 1)[-1]


def _usage_from(response) -> dict:
    meta = getattr(response, "usage_metadata", None)

    def count(field):
        return int(getattr(meta, field, None) or 0)

    return {
        "prompt_tokens": count("prompt_token_count"),
        "output_tokens": count("candidates_token_count"),
        "thinking_tokens": count("thoughts_token_count"),
        "total_tokens": count("total_token_count"),
        "model": f"{_cfg.provider}:{_cfg.model}",
        "estimated": False,
    }


def generate_answer(prompt: str) -> LLMAnswer:
    """
    Ask the LLM. Runs on the scheduler worker thread, one request at a time.
    Raises LLMUnavailable when there is no usable answer.
    """
    if client is None:
        text = _mock_template.format(prompt=prompt)
        prompt_tokens, output_tokens = _estimate_tokens(prompt), _estimate_tokens(text)
        return LLMAnswer(
            text=text, cacheable=True, finish_reason="STOP",
            usage={
                "prompt_tokens": prompt_tokens, "output_tokens": output_tokens,
                "thinking_tokens": 0, "total_tokens": prompt_tokens + output_tokens,
                "model": "mock", "estimated": True,
            },
        )
    try:
        response = client.models.generate_content(
            model=_cfg.model,
            contents=prompt,
            config=_generation_config()
        )
    except errors.APIError as exc:
        if exc.code == 429:
            raise LLMUnavailable(
                f"Gemini rate limit reached ({exc.message})", rate_limited=True
            ) from exc
        raise LLMUnavailable(f"Gemini error {exc.code} {exc.status}: {exc.message}") from exc
    except Exception as exc:
        # Timeouts and network failures
        raise LLMUnavailable(f"Could not reach Gemini: {exc}") from exc

    answer = response.text
    if not answer or not answer.strip():
        # Blocked by a safety filter, or the token limit was used up by thinking
        feedback = response.prompt_feedback
        if feedback and feedback.block_reason:
            reason = f"prompt blocked: {feedback.block_reason}"
        elif response.candidates:
            reason = f"finish reason: {response.candidates[0].finish_reason}"
        else:
            reason = "empty response"
        raise LLMUnavailable(f"Gemini returned no answer ({reason})")

    candidates = response.candidates
    finish_reason = _finish_name(candidates[0]) if candidates else None
    return LLMAnswer(
        text=answer.strip(),
        cacheable=finish_reason in (None, "STOP"),
        finish_reason=finish_reason,
        usage=_usage_from(response),
    )
