"""Tests for QualityEvaluator._call_openai retry logic."""

import logging
import sys
import types
from unittest.mock import MagicMock, patch

import pytest


class _RateLimitError(Exception):
    pass

class _APITimeoutError(Exception):
    pass

class _APIConnectionError(Exception):
    pass

class _InternalServerError(Exception):
    pass

_openai_stub = types.ModuleType("openai")
_openai_stub.RateLimitError = _RateLimitError
_openai_stub.APITimeoutError = _APITimeoutError
_openai_stub.APIConnectionError = _APIConnectionError
_openai_stub.InternalServerError = _InternalServerError
_openai_stub.OpenAI = MagicMock()
sys.modules.setdefault("openai", _openai_stub)

import quality_evaluator  # noqa: E402
from quality_evaluator import QualityEvaluator, _MAX_RETRIES  # noqa: E402

quality_evaluator._RETRYABLE = (
    _RateLimitError,
    _APITimeoutError,
    _APIConnectionError,
    _InternalServerError,
)


def _make_evaluator() -> QualityEvaluator:
    return QualityEvaluator(llm_provider="openai", api_key="test-key")


def _make_openai_response(content: str) -> MagicMock:
    response = MagicMock()
    response.choices[0].message.content = content
    return response


@pytest.fixture
def evaluator():
    return _make_evaluator()


@patch("quality_evaluator.time.sleep")
@patch("quality_evaluator.OpenAI")
def test_success_on_first_attempt(mock_openai_cls, mock_sleep, evaluator):
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.chat.completions.create.return_value = _make_openai_response('{"ok": true}')

    result = evaluator._call_openai("prompt")

    assert result == '{"ok": true}'
    assert mock_client.chat.completions.create.call_count == 1
    mock_sleep.assert_not_called()


@pytest.mark.parametrize(
    "error_cls",
    [_RateLimitError, _APITimeoutError, _APIConnectionError, _InternalServerError],
    ids=["RateLimitError", "APITimeoutError", "APIConnectionError", "InternalServerError"],
)
@patch("quality_evaluator.time.sleep")
@patch("quality_evaluator.OpenAI")
def test_retries_on_transient_error_then_succeeds(
    mock_openai_cls, mock_sleep, error_cls, evaluator
):
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.chat.completions.create.side_effect = [
        error_cls(),
        error_cls(),
        _make_openai_response('{"result": "ok"}'),
    ]

    result = evaluator._call_openai("prompt")

    assert result == '{"result": "ok"}'
    assert mock_client.chat.completions.create.call_count == 3
    assert mock_sleep.call_count == 2


@patch("quality_evaluator.time.sleep")
@patch("quality_evaluator.OpenAI")
def test_exhausts_all_retries_and_returns_none(mock_openai_cls, mock_sleep, evaluator):
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.chat.completions.create.side_effect = _RateLimitError()

    result = evaluator._call_openai("prompt")

    assert result is None
    assert mock_client.chat.completions.create.call_count == _MAX_RETRIES
    assert mock_sleep.call_count == _MAX_RETRIES


@patch("quality_evaluator.time.sleep")
@patch("quality_evaluator.OpenAI")
def test_logs_warning_on_each_retry(mock_openai_cls, mock_sleep, evaluator, caplog):
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.chat.completions.create.side_effect = _RateLimitError()

    with caplog.at_level(logging.WARNING, logger="quality_evaluator"):
        evaluator._call_openai("prompt")

    warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warning_messages) == _MAX_RETRIES
    assert all("retrying" in m for m in warning_messages)


@patch("quality_evaluator.time.sleep")
@patch("quality_evaluator.OpenAI")
def test_logs_error_after_all_retries_exhausted(mock_openai_cls, mock_sleep, evaluator, caplog):
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.chat.completions.create.side_effect = _RateLimitError()

    with caplog.at_level(logging.ERROR, logger="quality_evaluator"):
        evaluator._call_openai("prompt")

    error_messages = [r.message for r in caplog.records if r.levelno == logging.ERROR]
    assert any("after" in m and "retries" in m for m in error_messages)


@patch("quality_evaluator.time.sleep")
@patch("quality_evaluator.OpenAI")
def test_exponential_backoff_increases(mock_openai_cls, mock_sleep, evaluator):
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.chat.completions.create.side_effect = _RateLimitError()

    evaluator._call_openai("prompt")

    sleep_durations = [call.args[0] for call in mock_sleep.call_args_list]
    for i, duration in enumerate(sleep_durations):
        assert duration >= 2**i, f"Attempt {i}: sleep {duration} < base {2**i}"
    assert sleep_durations[-1] > sleep_durations[0]


@patch("quality_evaluator.time.sleep")
@patch("quality_evaluator.OpenAI")
def test_returns_none_immediately_on_non_retryable_error(
    mock_openai_cls, mock_sleep, evaluator
):
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.chat.completions.create.side_effect = ValueError("unexpected")

    result = evaluator._call_openai("prompt")

    assert result is None
    assert mock_client.chat.completions.create.call_count == 1
    mock_sleep.assert_not_called()


@patch("quality_evaluator.time.sleep")
@patch("quality_evaluator.OpenAI")
def test_logs_error_on_non_retryable_error(mock_openai_cls, mock_sleep, evaluator, caplog):
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.chat.completions.create.side_effect = ValueError("unexpected")

    with caplog.at_level(logging.ERROR, logger="quality_evaluator"):
        evaluator._call_openai("prompt")

    assert any("OpenAI API failed" in r.message for r in caplog.records)


@patch("quality_evaluator.time.sleep")
@patch("quality_evaluator.OpenAI")
def test_uses_configured_model(mock_openai_cls, mock_sleep, evaluator):
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.chat.completions.create.return_value = _make_openai_response("{}")

    evaluator._call_openai("prompt")

    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert call_kwargs["model"] == evaluator.openai_model


@patch("quality_evaluator.time.sleep")
@patch("quality_evaluator.OpenAI")
def test_temperature_is_zero(mock_openai_cls, mock_sleep, evaluator):
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.chat.completions.create.return_value = _make_openai_response("{}")

    evaluator._call_openai("prompt")

    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert call_kwargs["temperature"] == 0


@patch("quality_evaluator.time.sleep")
@patch("quality_evaluator.OpenAI")
def test_prompt_is_passed_as_user_message(mock_openai_cls, mock_sleep, evaluator):
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.chat.completions.create.return_value = _make_openai_response("{}")

    evaluator._call_openai("my test prompt")

    messages = mock_client.chat.completions.create.call_args.kwargs["messages"]
    user_messages = [m for m in messages if m["role"] == "user"]
    assert len(user_messages) == 1
    assert user_messages[0]["content"] == "my test prompt"
