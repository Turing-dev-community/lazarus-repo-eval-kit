"""Unit tests for fetch_repo_public_signals across all three platform clients."""

from unittest.mock import Mock, patch

import requests

from eval_kit.platform_clients import (
    BitbucketClient,
    GitHubClient,
    GitLabClient,
    _check_wayback_machine,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _http_error(status: int) -> requests.exceptions.HTTPError:
    mock_resp = Mock(spec=requests.Response)
    mock_resp.status_code = status
    return requests.exceptions.HTTPError(response=mock_resp)


def _wayback_hit():
    return {
        "archived_snapshots": {
            "closest": {"available": True, "status": "200", "timestamp": "20230101"}
        }
    }


def _wayback_miss():
    return {"archived_snapshots": {}}


# ── _check_wayback_machine ────────────────────────────────────────────────────


def test_wayback_returns_true_on_200_snapshot():
    with patch("eval_kit.platform_clients.requests.get") as mock_get:
        mock_get.return_value = Mock(
            json=lambda: _wayback_hit(), raise_for_status=lambda: None
        )
        assert _check_wayback_machine("github.com/foo/bar") is True


def test_wayback_returns_false_when_not_archived():
    with patch("eval_kit.platform_clients.requests.get") as mock_get:
        mock_get.return_value = Mock(
            json=lambda: _wayback_miss(), raise_for_status=lambda: None
        )
        assert _check_wayback_machine("github.com/foo/bar") is False


def test_wayback_returns_false_on_non_200_snapshot():
    data = {"archived_snapshots": {"closest": {"available": True, "status": "404"}}}
    with patch("eval_kit.platform_clients.requests.get") as mock_get:
        mock_get.return_value = Mock(json=lambda: data, raise_for_status=lambda: None)
        assert _check_wayback_machine("github.com/foo/bar") is False


def test_wayback_returns_false_on_network_error():
    with patch(
        "eval_kit.platform_clients.requests.get", side_effect=requests.ConnectionError
    ):
        assert _check_wayback_machine("github.com/foo/bar") is False


# ── GitHubClient ──────────────────────────────────────────────────────────────


def _github_client():
    return GitHubClient(owner="acme", repo_name="widget", token="tok")


def _github_graphql_response(is_private: bool, forks: int = 0, stars: int = 0):
    return {
        "data": {
            "repository": {
                "isPrivate": is_private,
                "forkCount": forks,
                "stargazerCount": stars,
            }
        }
    }


@patch("eval_kit.platform_clients._check_wayback_machine", return_value=False)
@patch("eval_kit.platform_clients.requests.post")
def test_github_public_repo_all_signals(mock_post, mock_wb):
    mock_post.return_value = Mock(
        json=lambda: _github_graphql_response(False, forks=10, stars=50),
        raise_for_status=lambda: None,
    )
    result = _github_client().fetch_repo_public_signals()
    assert result["state"] == "positive"
    assert "currently_public" in result["signals"]
    assert "has_forks:10" in result["signals"]
    assert "has_stars:50" in result["signals"]


@patch("eval_kit.platform_clients._check_wayback_machine", return_value=False)
@patch("eval_kit.platform_clients.requests.post")
def test_github_private_repo_no_forks_stars(mock_post, mock_wb):
    """Private repo: forks/stars must NOT fire even if counts > 0 (org-internal forks)."""
    mock_post.return_value = Mock(
        json=lambda: _github_graphql_response(True, forks=3, stars=5),
        raise_for_status=lambda: None,
    )
    result = _github_client().fetch_repo_public_signals()
    assert result["state"] == "neutral"
    assert result["signals"] == []


@patch("eval_kit.platform_clients._check_wayback_machine", return_value=True)
@patch("eval_kit.platform_clients.requests.post")
def test_github_private_repo_wayback_hit(mock_post, mock_wb):
    mock_post.return_value = Mock(
        json=lambda: _github_graphql_response(True),
        raise_for_status=lambda: None,
    )
    result = _github_client().fetch_repo_public_signals()
    assert result["state"] == "positive"
    assert result["signals"] == ["wayback_snapshot"]


@patch("eval_kit.platform_clients._check_wayback_machine", return_value=False)
@patch("eval_kit.platform_clients.requests.post")
def test_github_auth_failure_returns_unknown(mock_post, mock_wb):
    mock_post.return_value = Mock(raise_for_status=Mock(side_effect=_http_error(401)))
    result = _github_client().fetch_repo_public_signals()
    assert result["state"] == "unknown"
    assert result["signals"] == []


@patch("eval_kit.platform_clients._check_wayback_machine", return_value=True)
@patch("eval_kit.platform_clients.requests.post")
def test_github_auth_failure_with_wayback_still_positive(mock_post, mock_wb):
    """Auth failure but Wayback Machine has a snapshot → still positive."""
    mock_post.return_value = Mock(raise_for_status=Mock(side_effect=_http_error(403)))
    result = _github_client().fetch_repo_public_signals()
    assert result["state"] == "positive"
    assert "wayback_snapshot" in result["signals"]


# ── BitbucketClient ───────────────────────────────────────────────────────────


def _bitbucket_client():
    return BitbucketClient(owner="acme", repo_name="widget", token="tok")


@patch("eval_kit.platform_clients._check_wayback_machine", return_value=False)
@patch("eval_kit.platform_clients.requests.get")
def test_bitbucket_public_repo(mock_get, mock_wb):
    mock_get.return_value = Mock(
        json=lambda: {"is_private": False},
        raise_for_status=lambda: None,
    )
    result = _bitbucket_client().fetch_repo_public_signals()
    assert result["state"] == "positive"
    assert "currently_public" in result["signals"]


@patch("eval_kit.platform_clients._check_wayback_machine", return_value=False)
@patch("eval_kit.platform_clients.requests.get")
def test_bitbucket_private_repo(mock_get, mock_wb):
    mock_get.return_value = Mock(
        json=lambda: {"is_private": True},
        raise_for_status=lambda: None,
    )
    result = _bitbucket_client().fetch_repo_public_signals()
    assert result["state"] == "neutral"


@patch("eval_kit.platform_clients._check_wayback_machine", return_value=False)
@patch("eval_kit.platform_clients.requests.get")
def test_bitbucket_auth_failure_returns_unknown(mock_get, mock_wb):
    mock_get.return_value = Mock(raise_for_status=Mock(side_effect=_http_error(401)))
    result = _bitbucket_client().fetch_repo_public_signals()
    assert result["state"] == "unknown"


# ── GitLabClient ──────────────────────────────────────────────────────────────


def _gitlab_client():
    return GitLabClient(owner="acme", repo_name="widget", token="tok")


@patch("eval_kit.platform_clients._check_wayback_machine", return_value=False)
@patch("eval_kit.platform_clients.requests.get")
def test_gitlab_public_repo_with_forks(mock_get, mock_wb):
    mock_get.return_value = Mock(
        json=lambda: {"visibility": "public", "forks_count": 7},
        raise_for_status=lambda: None,
    )
    result = _gitlab_client().fetch_repo_public_signals()
    assert result["state"] == "positive"
    assert "currently_public" in result["signals"]
    assert "has_forks:7" in result["signals"]


@patch("eval_kit.platform_clients._check_wayback_machine", return_value=False)
@patch("eval_kit.platform_clients.requests.get")
def test_gitlab_internal_visibility_not_flagged(mock_get, mock_wb):
    """GitLab 'internal' is not the public internet — must NOT produce currently_public."""
    mock_get.return_value = Mock(
        json=lambda: {"visibility": "internal", "forks_count": 3},
        raise_for_status=lambda: None,
    )
    result = _gitlab_client().fetch_repo_public_signals()
    assert result["state"] == "neutral"
    assert "currently_public" not in result["signals"]


@patch("eval_kit.platform_clients._check_wayback_machine", return_value=False)
@patch("eval_kit.platform_clients.requests.get")
def test_gitlab_private_repo_forks_not_flagged(mock_get, mock_wb):
    """Private GitLab repo with forks must not produce has_forks signal."""
    mock_get.return_value = Mock(
        json=lambda: {"visibility": "private", "forks_count": 5},
        raise_for_status=lambda: None,
    )
    result = _gitlab_client().fetch_repo_public_signals()
    assert result["state"] == "neutral"
    assert result["signals"] == []


@patch("eval_kit.platform_clients._check_wayback_machine", return_value=False)
@patch("eval_kit.platform_clients.requests.get")
def test_gitlab_auth_failure_returns_unknown(mock_get, mock_wb):
    mock_get.return_value = Mock(raise_for_status=Mock(side_effect=_http_error(403)))
    result = _gitlab_client().fetch_repo_public_signals()
    assert result["state"] == "unknown"
