import logging
import os
import random
import time

from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)

RETRYABLE_ERRORS = (RateLimitError, APITimeoutError, APIConnectionError, InternalServerError)
MAX_RETRIES = int(os.environ.get("LLM_MAX_RETRIES", "8"))
BASE_DELAY = float(os.environ.get("LLM_BACKOFF_BASE_DELAY", "5.0"))

logger = logging.getLogger(__name__)


def call_llm(
    messages: list[dict],
    *,
    model: str,
    client: OpenAI | None = None,
    api_key: str | None = None,
    base_url: str = "https://api.openai.com/v1",
    temperature: float = 0,
    max_retries: int = MAX_RETRIES,
    base_delay: float = BASE_DELAY,
    response_format=None,
) -> str | object:
    """Call an OpenAI-compatible LLM with exponential-backoff retry logic.

    Set response_format to a Pydantic model to use structured output
    (beta.chat.completions.parse); omit for plain string content.

    Raises on exhausted retries or non-retryable errors — callers are
    responsible for their own fallback/sentinel values.
    """
    if client is None:
        client = OpenAI(api_key=api_key, base_url=base_url)

    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            if response_format is not None:
                response = client.beta.chat.completions.parse(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    response_format=response_format,
                )
                return response.choices[0].message.parsed
            else:
                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                )
                return response.choices[0].message.content
        except RETRYABLE_ERRORS as e:
            last_err = e
            delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
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
