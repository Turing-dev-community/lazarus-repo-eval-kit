"""Tests for 504 Gateway Timeout page-size reduction in fetch_prs."""

import logging

import pytest
import requests
from unittest.mock import Mock, patch

from platform_clients import (
    GitHubClient,
    BitbucketClient,
    GitLabClient,
    MIN_PAGE_SIZE,
    MAX_RETRIES,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_504_response():
    """Create a mock response that raises HTTPError with 504."""
    mock_response = Mock(spec=requests.Response)
    mock_response.status_code = 504
    mock_response.headers = {}
    mock_response.text = "Gateway Timeout"
    mock_response.raise_for_status.side_effect = requests.exceptions.HTTPError(
        response=mock_response
    )
    return mock_response


def _make_success_github_response(nodes=None):
    """Create a mock successful GitHub GraphQL response."""
    if nodes is None:
        nodes = []
    data = {
        "data": {
            "repository": {
                "pullRequests": {
                    "pageInfo": {"endCursor": None, "hasNextPage": False},
                    "nodes": nodes,
                }
            }
        }
    }
    mock_response = Mock(spec=requests.Response)
    mock_response.status_code = 200
    mock_response.headers = {}
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = data
    return mock_response


def _make_success_bitbucket_response(values=None, next_url=None):
    """Create a mock successful Bitbucket API response."""
    if values is None:
        values = []
    data = {"values": values, "next": next_url}
    mock_response = Mock(spec=requests.Response)
    mock_response.status_code = 200
    mock_response.headers = {}
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = data
    return mock_response


def _make_success_gitlab_response(data=None):
    """Create a mock successful GitLab API response."""
    if data is None:
        data = []
    mock_response = Mock(spec=requests.Response)
    mock_response.status_code = 200
    mock_response.headers = {"X-Next-Page": ""}
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = data
    return mock_response


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def github_client():
    return GitHubClient(owner="test-owner", repo_name="test-repo", token="test-token")


@pytest.fixture
def bitbucket_client():
    return BitbucketClient(
        owner="test-owner", repo_name="test-repo", token="test-token"
    )


@pytest.fixture
def gitlab_client():
    return GitLabClient(owner="test-owner", repo_name="test-repo", token="test-token")


# ── GitHub tests ─────────────────────────────────────────────────────────────


@patch("platform_clients.time.sleep")
@patch("platform_clients.requests.post")
def test_github_fetch_prs_halves_page_size_on_504(mock_post, mock_sleep, github_client):
    """504 at page_size=50 → halves to 25 → succeeds."""
    call_log = []

    def side_effect(url, **kwargs):
        page_size = kwargs["json"]["variables"]["page_size"]
        call_log.append(page_size)
        if page_size > 25:
            return _make_504_response()
        return _make_success_github_response()

    mock_post.side_effect = side_effect

    result = github_client.fetch_prs(page_size=50)

    assert result is not None

    fifty_count = call_log.count(50)
    twenty_five_count = call_log.count(25)

    assert fifty_count == MAX_RETRIES + 1, (
        f"Expected {MAX_RETRIES + 1} attempts at page_size=50, got {fifty_count}"
    )
    assert twenty_five_count == 1, (
        f"Expected exactly 1 attempt at page_size=25, got {twenty_five_count}"
    )


@patch("platform_clients.time.sleep")
@patch("platform_clients.requests.post")
def test_github_fetch_prs_raises_at_min_page_size(mock_post, mock_sleep, github_client):
    """504 at MIN_PAGE_SIZE should raise, not loop forever."""
    mock_post.return_value = _make_504_response()

    with pytest.raises(requests.exceptions.HTTPError):
        github_client.fetch_prs(page_size=MIN_PAGE_SIZE)

    assert mock_post.call_count == MAX_RETRIES + 1


@patch("platform_clients.time.sleep")
@patch("platform_clients.requests.post")
def test_github_fetch_prs_logs_giving_up_at_min_page_size(
    mock_post, mock_sleep, github_client, caplog
):
    """504 at MIN_PAGE_SIZE should log a 'giving up' message."""
    mock_post.return_value = _make_504_response()

    with caplog.at_level(logging.WARNING):
        with pytest.raises(requests.exceptions.HTTPError):
            github_client.fetch_prs(page_size=MIN_PAGE_SIZE)

    assert any(
        "giving up page-size backoff" in record.message for record in caplog.records
    )


@patch("platform_clients.time.sleep")
@patch("platform_clients.requests.post")
def test_github_fetch_prs_succeeds_first_try(mock_post, mock_sleep, github_client):
    """No 504 → should succeed on the first attempt without page-size reduction."""
    mock_post.return_value = _make_success_github_response()

    result = github_client.fetch_prs(page_size=50)

    assert result is not None
    assert mock_post.call_count == 1


@patch("platform_clients.time.sleep")
@patch("platform_clients.requests.post")
def test_github_fetch_prs_multiple_halvings(mock_post, mock_sleep, github_client):
    """504 at page_size=50 → 25 → 12 → succeeds."""
    call_log = []

    def side_effect(url, **kwargs):
        page_size = kwargs["json"]["variables"]["page_size"]
        call_log.append(page_size)
        if page_size > 12:
            return _make_504_response()
        return _make_success_github_response()

    mock_post.side_effect = side_effect

    result = github_client.fetch_prs(page_size=50)

    assert result is not None

    fifty_count = call_log.count(50)
    twenty_five_count = call_log.count(25)
    twelve_count = call_log.count(12)

    assert fifty_count == MAX_RETRIES + 1
    assert twenty_five_count == MAX_RETRIES + 1
    assert twelve_count == 1


# ── Bitbucket tests ──────────────────────────────────────────────────────────


@patch("platform_clients.time.sleep")
@patch("platform_clients.requests.get")
def test_bitbucket_fetch_prs_halves_page_size_on_504(
    mock_get, mock_sleep, bitbucket_client
):
    """504 at page_size=50 → halves to 25 → succeeds."""
    call_log = []

    def side_effect(url, **kwargs):
        params = kwargs.get("params") or {}
        page_size = params.get("pagelen", 0)
        call_log.append(page_size)
        if page_size > 25:
            return _make_504_response()
        return _make_success_bitbucket_response()

    mock_get.side_effect = side_effect

    result = bitbucket_client.fetch_prs(page_size=50)

    assert result is not None

    fifty_count = call_log.count(50)
    twenty_five_count = call_log.count(25)

    assert fifty_count == MAX_RETRIES + 1
    assert twenty_five_count == 1


@patch("platform_clients.time.sleep")
@patch("platform_clients.requests.get")
def test_bitbucket_fetch_prs_no_backoff_for_cursor_url(
    mock_get, mock_sleep, bitbucket_client
):
    """When cursor is a full URL (next page link), can't reduce page size → raises."""

    def side_effect(url, **kwargs):
        return _make_504_response()

    mock_get.side_effect = side_effect

    with pytest.raises(requests.exceptions.HTTPError):
        bitbucket_client.fetch_prs(
            cursor="https://api.bitbucket.org/2.0/repositories/...?page=2"
        )


# ── GitLab tests ─────────────────────────────────────────────────────────────


@patch("platform_clients.time.sleep")
@patch("platform_clients.requests.get")
def test_gitlab_fetch_prs_halves_page_size_on_504(mock_get, mock_sleep, gitlab_client):
    """504 at page_size=50 → halves to 25 → succeeds."""
    call_log = []

    def side_effect(url, **kwargs):
        params = kwargs.get("params") or {}
        page_size = params.get("per_page", 0)
        call_log.append(page_size)
        if page_size > 25:
            return _make_504_response()
        return _make_success_gitlab_response()

    mock_get.side_effect = side_effect

    result = gitlab_client.fetch_prs(page_size=50)

    assert result is not None

    fifty_count = call_log.count(50)
    twenty_five_count = call_log.count(25)

    assert fifty_count == MAX_RETRIES + 1
    assert twenty_five_count == 1


@patch("platform_clients.time.sleep")
@patch("platform_clients.requests.get")
def test_gitlab_fetch_prs_raises_at_min_page_size(mock_get, mock_sleep, gitlab_client):
    """504 at MIN_PAGE_SIZE should raise."""
    mock_get.return_value = _make_504_response()

    with pytest.raises(requests.exceptions.HTTPError):
        gitlab_client.fetch_prs(page_size=MIN_PAGE_SIZE)

    assert mock_get.call_count == MAX_RETRIES + 1


@patch("platform_clients.time.sleep")
@patch("platform_clients.requests.get")
def test_gitlab_fetch_prs_logs_giving_up_at_min_page_size(
    mock_get, mock_sleep, gitlab_client, caplog
):
    """504 at MIN_PAGE_SIZE should log 'giving up'."""
    mock_get.return_value = _make_504_response()

    with caplog.at_level(logging.WARNING):
        with pytest.raises(requests.exceptions.HTTPError):
            gitlab_client.fetch_prs(page_size=MIN_PAGE_SIZE)

    assert any(
        "giving up page-size backoff" in record.message for record in caplog.records
    )
