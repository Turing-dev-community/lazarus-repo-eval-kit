import logging
import os
import random
import time

import genai_prices
import httpx
from pydantic_ai import Agent

from eval_kit.usage_tracker import CostLimitAborted, get_tracker

MAX_RETRIES = int(os.environ.get("LLM_MAX_RETRIES", "8"))
BASE_DELAY = float(os.environ.get("LLM_BACKOFF_BASE_DELAY", "5.0"))

PROVIDER_PREFIXES = {
    "openai": "openai",
    "anthropic": "anthropic",
    "google": "google-gla",
}
API_KEY_ENV_VARS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
}
DEFAULT_MODELS = {
    "openai": "gpt-5.1",
    "anthropic": "claude-sonnet-4-6",
    "google": "gemini-3-flash-preview",
}
RETRYABLE_ERRORS: tuple = (httpx.ConnectError, httpx.TimeoutException)

logger = logging.getLogger(__name__)


def validate_api_key(provider: str) -> None:
    env_var = API_KEY_ENV_VARS.get(provider, "OPENAI_API_KEY")
    if not os.getenv(env_var, ""):
        raise ValueError(
            f"{env_var} is not set. Set it in your .env file or environment."
        )


def build_model_string(provider: str) -> str:
    """Return pydantic-ai model identifier for the given provider.

    Resolution order:
    1. LLM_MODEL env var (explicit override, model name only, no prefix)
    2. DEFAULT_MODELS[provider] (built-in per-provider default)
    """
    model_name = os.getenv("LLM_MODEL") or DEFAULT_MODELS.get(provider, "gpt-4o")
    prefix = PROVIDER_PREFIXES.get(provider, "openai")
    return f"{prefix}:{model_name}"


def _track_cost(result, model_str: str, provider: str) -> None:
    """Compute the USD cost of one agent run and forward it to the tracker.

    Best-effort: any pricing failure is logged and silently ignored so that
    a missing model entry never breaks the evaluation.
    """
    try:
        model_name = model_str.split(":", 1)[1] if ":" in model_str else model_str
        price = genai_prices.calc_price(
            result.usage(), model_name, provider_id=provider
        )
        get_tracker().add_cost(price.total_price)
    except CostLimitAborted:
        raise
    except Exception as exc:
        logger.debug("Cost tracking failed for %s: %s", model_str, exc)


def call_llm(
    messages: list[dict],
    *,
    provider: str | None = None,
    temperature: float = 0,
    max_retries: int = MAX_RETRIES,
    base_delay: float = BASE_DELAY,
    response_format=None,
) -> str | object:
    """Call an LLM via pydantic-ai with exponential-backoff retry logic.

    Provider: `provider` arg > LLM_PROVIDER env var > "openai".
    Model: LLM_MODEL env var > built-in DEFAULT_MODELS[provider].
    Structured output: pass a Pydantic model class as response_format.
    """
    effective_provider = (provider or os.environ.get("LLM_PROVIDER", "openai")).lower()
    validate_api_key(effective_provider)

    system_prompt = ""
    user_parts: list[str] = []
    for msg in messages:
        if msg["role"] == "system":
            system_prompt = msg["content"]
        else:
            user_parts.append(msg["content"])
    user_prompt = "\n\n".join(user_parts)

    model_str = build_model_string(effective_provider)

    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            if response_format is not None:
                agent = Agent(
                    model_str,
                    system_prompt=system_prompt,
                    output_type=response_format,
                )
            else:
                agent = Agent(model_str, system_prompt=system_prompt)
            result = agent.run_sync(
                user_prompt, model_settings={"temperature": temperature}
            )
            _track_cost(result, model_str, effective_provider)
            return result.output
        except RETRYABLE_ERRORS as e:
            last_err = e
            delay = base_delay * (2**attempt) + random.uniform(0, 1)
            logger.warning(
                f"LLM call failed (attempt {attempt + 1}/{max_retries}): "
                f"{type(e).__name__} — retrying in {delay:.1f}s"
            )
            time.sleep(delay)
        except Exception as e:
            logger.error(f"LLM call failed with non-retryable error: {e}")
            raise

    logger.error(f"LLM call failed after {max_retries} retries: {last_err}")
    raise last_err
