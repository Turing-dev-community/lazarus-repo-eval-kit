import logging
import re
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Callable, Dict, List, Optional

import requests

from eval_kit.repo_evaluator_helpers import HEADERS

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY_BASE = 1
MIN_PAGE_SIZE = 5


def retry_api_call(func: Callable, max_retries: int = MAX_RETRIES, *args, **kwargs):
    retries = 0
    last_exception = None

    while retries <= max_retries:
        try:
            return func(*args, **kwargs)
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if e.response else 0
            if status_code == 429:
                retry_after = e.response.headers.get(
                    "X-RateLimit-Reset",
                    e.response.headers.get("Retry-After", 60),
                )
                try:
                    wait_time = int(retry_after)
                except ValueError:
                    wait_time = 60
                logger.warning(f"Rate limit hit. Waiting {wait_time} seconds...")
                time.sleep(wait_time)
                retries += 1
                last_exception = e
                continue
            if 500 <= status_code < 600 and retries < max_retries:
                wait_time = RETRY_DELAY_BASE * (2**retries)
                logger.warning(
                    f"Server error {status_code}. Retrying in {wait_time} seconds..."
                )
                time.sleep(wait_time)
                retries += 1
                last_exception = e
                continue
            raise
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if retries < max_retries:
                wait_time = RETRY_DELAY_BASE * (2**retries)
                logger.warning(f"Connection error. Retrying in {wait_time} seconds...")
                time.sleep(wait_time)
                retries += 1
                last_exception = e
                continue
            raise
        except Exception as e:
            if retries < max_retries:
                wait_time = RETRY_DELAY_BASE * (2**retries)
                logger.warning(
                    f"Unexpected error: {str(e)}. Retrying in {wait_time} seconds..."
                )
                time.sleep(wait_time)
                retries += 1
                last_exception = e
                continue
            raise

    if last_exception:
        raise last_exception


def detect_platform(repo_string: str, explicit_platform: Optional[str] = "auto") -> str:
    if explicit_platform:
        explicit_platform = explicit_platform.lower()
        if explicit_platform in ["github", "bitbucket", "gitlab"]:
            return explicit_platform
        if explicit_platform != "auto":
            raise ValueError(
                f"Invalid platform: {explicit_platform}. Must be 'github', 'bitbucket', 'gitlab', or 'auto'"
            )

    repo_string = repo_string.strip().lower()
    if repo_string.startswith("bitbucket:"):
        return "bitbucket"
    if repo_string.startswith("github:"):
        return "github"
    if repo_string.startswith("gitlab:"):
        return "gitlab"

    if "bitbucket.org" in repo_string:
        return "bitbucket"
    if "gitlab." in repo_string or "gitlab/" in repo_string:
        return "gitlab"
    if "github.com" in repo_string:
        return "github"
    return "github"


def _is_bot_username(username: str) -> bool:
    if not username:
        return False
    username_lower = username.lower()
    if username.endswith("[bot]"):
        return True
    common_bots = [
        "dependabot",
        "renovate",
        "codecov",
        "greenkeeper",
        "snyk-bot",
        "pyup-bot",
        "whitesource",
        "mergify",
        "stale",
        "github-actions",
        "allcontributors",
        "imgbot",
        "k8s-ci-robot",
        "k8s-bot",
        "k8s-mergebot",
    ]
    return username_lower in common_bots


class PlatformClient(ABC):
    def __init__(self, owner: str, repo_name: str, token: Optional[str] = None):
        self.owner = owner
        self.repo_name = repo_name
        self.repo_full_name = f"{owner}/{repo_name}"
        self.token = token

    @abstractmethod
    def fetch_prs(
        self,
        cursor: Optional[str] = None,
        page_size: int = 50,
        start_date: Optional[datetime] = None,
    ) -> dict:
        pass

    @abstractmethod
    def fetch_issue(self, issue_number: int) -> Optional[dict]:
        pass

    @abstractmethod
    def get_repo_url(self, include_token: bool = False) -> str:
        pass

    @abstractmethod
    def extract_issue_number_from_text(self, text: str) -> List[int]:
        pass

    @abstractmethod
    def fetch_repo_languages(self) -> Optional[Dict[str, int]]:
        pass

    @abstractmethod
    def fetch_issue_count(self) -> dict:
        pass

    @abstractmethod
    def fetch_patch(self, base_commit: str, head_commit: str) -> Optional[str]:
        pass


class GitHubClient(PlatformClient):
    def __init__(self, owner: str, repo_name: str, token: Optional[str] = None):
        super().__init__(owner, repo_name, token)
        self.base_url = "https://api.github.com"
        self.headers = HEADERS.copy()
        if self.token:
            self.headers["Authorization"] = f"Bearer {self.token}"

    def fetch_prs(
        self,
        cursor: Optional[str] = None,
        page_size: int = 50,
        start_date: Optional[datetime] = None,
    ) -> dict:
        query = """
            query($owner: String!, $name: String!, $cursor: String, $page_size: Int!) {
            repository(owner: $owner, name: $name) {
              primaryLanguage { name }
              owner { login }
              name
              pullRequests(
                first: $page_size,
                after: $cursor,
                orderBy: {field: CREATED_AT, direction: DESC}
              ) {
                pageInfo {
                  endCursor
                  hasNextPage
                }
                nodes {
                  number
                  title
                  body
                  baseRefOid
                  headRefOid
                  baseRefName
                  headRefName
                  mergedAt
                  createdAt
                  url
                  author {
                    login
                    __typename
                  }
                  files(first: 100) {
                    nodes {
                      path
                      changeType
                      additions
                      deletions
                    }
                  }
                  closingIssuesReferences(first: 10) {
                    nodes {
                      url
                      number
                      title
                      body
                      state
                      __typename
                    }
                  }
                  labels(first: 10) {
                    nodes {
                      name
                    }
                  }
                }
              }
            }
          }
        """
        query_string = f"repo:{self.owner}/{self.repo_name} is:pr is:merged"
        if start_date:
            query_string += f" merged:>={start_date}"

        current_page_size = page_size
        while True:
            variables = {
                "owner": self.owner,
                "name": self.repo_name,
                "queryString": query_string,
                "cursor": cursor,
                "page_size": current_page_size,
            }

            def _make_request():
                response = requests.post(
                    f"{self.base_url}/graphql",
                    json={"query": query, "variables": variables},
                    headers=self.headers,
                    timeout=30,
                )
                response.raise_for_status()
                return response.json()

            try:
                return retry_api_call(_make_request)
            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 504:
                    if current_page_size > MIN_PAGE_SIZE:
                        current_page_size = current_page_size // 2
                        logger.warning(
                            f"504 Gateway Timeout with page_size={current_page_size * 2}, "
                            f"retrying with page_size={current_page_size}"
                        )
                        continue
                    logger.warning(
                        f"504 Gateway Timeout at minimum page_size={current_page_size}, "
                        f"giving up page-size backoff"
                    )
                raise

    def fetch_issue(self, issue_number: int) -> Optional[dict]:
        try:

            def _make_request():
                response = requests.get(
                    f"{self.base_url}/repos/{self.repo_full_name}/issues/{issue_number}",
                    headers=self.headers,
                    timeout=30,
                )
                response.raise_for_status()
                return response.json()

            issue_details = retry_api_call(_make_request)
            if "pull_request" in issue_details:
                return None

            return {
                "number": issue_details.get("number"),
                "title": issue_details.get("title", ""),
                "body": issue_details.get("body", ""),
                "state": issue_details.get("state", "").upper(),
                "__typename": "Issue",
            }
        except Exception:
            return None

    def fetch_issue_count(self) -> dict:
        query = """
            query($owner: String!, $name: String!) {
                repository(owner: $owner, name: $name) {
                    open: issues(states: OPEN) { totalCount }
                    closed: issues(states: CLOSED) { totalCount }
                }
            }
        """
        variables = {"owner": self.owner, "name": self.repo_name}

        def _make_request():
            response = requests.post(
                f"{self.base_url}/graphql",
                json={"query": query, "variables": variables},
                headers=self.headers,
                timeout=30,
            )
            response.raise_for_status()
            return response.json()

        result = retry_api_call(_make_request)
        repo = result.get("data", {}).get("repository", {})
        open_count = repo.get("open", {}).get("totalCount", 0)
        closed_count = repo.get("closed", {}).get("totalCount", 0)
        return {
            "open": open_count,
            "closed": closed_count,
            "total": open_count + closed_count,
        }

    def get_repo_url(self, include_token: bool = False) -> str:
        if include_token and self.token:
            return f"https://{self.token}@github.com/{self.repo_full_name}.git"
        return f"https://github.com/{self.repo_full_name}.git"

    def extract_issue_number_from_text(self, text: str) -> List[int]:
        if not text:
            return []
        issue_numbers = []
        issue_numbers.extend([int(m) for m in re.findall(r"#(\d+)", text)])
        issue_numbers.extend(
            [
                int(m)
                for m in re.findall(
                    r"https://github\.com/[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+/issues/(\d+)",
                    text,
                )
            ]
        )
        return list(set(issue_numbers))

    def fetch_repo_languages(self) -> Optional[Dict[str, int]]:
        try:
            url = f"{self.base_url}/repos/{self.repo_full_name}/languages"

            def _make_request():
                response = requests.get(url, headers=self.headers, timeout=30)
                response.raise_for_status()
                return response

            response = retry_api_call(_make_request)
            languages = response.json()
            return languages if languages else None
        except Exception as e:
            logger.debug(f"Failed to fetch repository languages from GitHub API: {e}")
            return None

    def fetch_patch(self, base_commit: str, head_commit: str) -> Optional[str]:
        diff_headers = self.headers.copy()
        diff_headers["Accept"] = "application/vnd.github.v3.diff"
        try:

            def _make_request():
                response = requests.get(
                    f"{self.base_url}/repos/{self.repo_full_name}/compare/{base_commit}...{head_commit}",
                    headers=diff_headers,
                    timeout=30,
                )
                response.raise_for_status()
                return response.text

            return retry_api_call(_make_request)
        except Exception:
            return None


class BitbucketClient(PlatformClient):
    def __init__(self, owner: str, repo_name: str, token: Optional[str] = None):
        super().__init__(owner, repo_name, token)
        self.base_url = "https://api.bitbucket.org/2.0"
        self.headers = {"Accept": "application/json"}
        if self.token:
            self.headers["Authorization"] = f"Bearer {self.token}"

    def fetch_prs(
        self,
        cursor: Optional[str] = None,
        page_size: int = 50,
        start_date: Optional[datetime] = None,
    ) -> dict:
        current_page_size = page_size
        if cursor and cursor.startswith("http"):
            request_url = cursor
            base_params = None
        else:
            request_url = f"{self.base_url}/repositories/{self.owner}/{self.repo_name}/pullrequests"
            base_params = {"state": "MERGED", "sort": "-created_on"}
            if cursor:
                base_params["page"] = cursor
            if start_date:
                base_params["q"] = f"created_on>={start_date.isoformat()}"

        while True:
            params = (
                {**base_params, "pagelen": current_page_size}
                if base_params is not None
                else None
            )

            def _make_request():
                response = requests.get(
                    request_url, headers=self.headers, params=params, timeout=30
                )
                response.raise_for_status()
                return response.json()

            try:
                data = retry_api_call(_make_request)
                break
            except requests.exceptions.HTTPError as e:
                if (
                    base_params is not None
                    and e.response is not None
                    and e.response.status_code == 504
                ):
                    if current_page_size > MIN_PAGE_SIZE:
                        current_page_size = current_page_size // 2
                        logger.warning(
                            f"504 Gateway Timeout with page_size={current_page_size * 2}, "
                            f"retrying with page_size={current_page_size}"
                        )
                        continue
                    logger.warning(
                        f"504 Gateway Timeout at minimum page_size={current_page_size}, "
                        f"giving up page-size backoff"
                    )
                raise
        pr_nodes = []
        for pr in data.get("values", []):
            files_url = pr.get("links", {}).get("diffstat", {}).get("href", "")
            files = []
            if files_url:
                try:

                    def _get_files():
                        files_response = requests.get(
                            files_url, headers=self.headers, timeout=30
                        )
                        files_response.raise_for_status()
                        return files_response.json()

                    files_data = retry_api_call(_get_files)
                    for file_info in files_data.get("values", []):
                        files.append(
                            {
                                "path": file_info.get("new", {}).get(
                                    "path", file_info.get("old", {}).get("path", "")
                                ),
                                "changeType": "ADDED"
                                if file_info.get("status") == "added"
                                else "DELETED"
                                if file_info.get("status") == "deleted"
                                else "MODIFIED",
                                "additions": file_info.get("lines_added", 0),
                                "deletions": file_info.get("lines_removed", 0),
                            }
                        )
                except Exception:
                    pass

            linked_issues = []
            issue_numbers = self.extract_issue_number_from_text(
                pr.get("description", "") or ""
            )
            for issue_num in issue_numbers:
                issue_data = self.fetch_issue(issue_num)
                if issue_data:
                    linked_issues.append(issue_data)

            author_info = pr.get("author", {}) or {}
            author_login = (
                author_info.get("display_name") or author_info.get("username") or ""
            )

            pr_nodes.append(
                {
                    "number": pr.get("id"),
                    "title": pr.get("title", ""),
                    "body": pr.get("description", "") or "",
                    "baseRefOid": pr.get("destination", {})
                    .get("commit", {})
                    .get("hash", ""),
                    "headRefOid": pr.get("source", {})
                    .get("commit", {})
                    .get("hash", ""),
                    "mergedAt": pr.get("closed_on", pr.get("updated_on", "")),
                    "createdAt": pr.get("created_on", ""),
                    "url": pr.get("links", {}).get("html", {}).get("href", ""),
                    "author": {
                        "login": author_login,
                        "isBot": _is_bot_username(author_login),
                        "__typename": "User",
                    },
                    "baseRepository": {
                        "nameWithOwner": f"{self.owner}/{self.repo_name}"
                    },
                    "headRepository": {
                        "nameWithOwner": f"{self.owner}/{self.repo_name}"
                    },
                    "files": {"nodes": files},
                    "closingIssuesReferences": {"nodes": linked_issues},
                    "labels": {"nodes": []},
                }
            )

        page_info = {
            "hasNextPage": data.get("next") is not None,
            "endCursor": data.get("next"),
        }
        primary_language_name = None
        try:
            languages = self.fetch_repo_languages()
            if languages:
                primary_language_name = list(languages.keys())[0]
        except Exception:
            pass

        return {
            "data": {
                "repository": {
                    "primaryLanguage": {"name": primary_language_name},
                    "owner": {"login": self.owner},
                    "name": self.repo_name,
                    "pullRequests": {"pageInfo": page_info, "nodes": pr_nodes},
                }
            }
        }

    def fetch_issue(self, issue_number: int) -> Optional[dict]:
        try:
            url = f"{self.base_url}/repositories/{self.owner}/{self.repo_name}/issues/{issue_number}"

            def _make_request():
                response = requests.get(url, headers=self.headers, timeout=30)
                response.raise_for_status()
                return response.json()

            issue_details = retry_api_call(_make_request)
            return {
                "number": issue_details.get("id"),
                "title": issue_details.get("title", ""),
                "body": issue_details.get("content", {}).get("raw", "")
                if isinstance(issue_details.get("content"), dict)
                else str(issue_details.get("content", "")),
                "state": issue_details.get("state", "").upper(),
                "__typename": "Issue",
            }
        except Exception:
            return None

    def get_repo_url(self, include_token: bool = False) -> str:
        if include_token and self.token:
            return f"https://x-token-auth:{self.token}@bitbucket.org/{self.repo_full_name}.git"
        return f"https://bitbucket.org/{self.repo_full_name}.git"

    def extract_issue_number_from_text(self, text: str) -> List[int]:
        if not text:
            return []
        issue_numbers = []
        issue_numbers.extend([int(m) for m in re.findall(r"#(\d+)", text)])
        issue_numbers.extend(
            [
                int(m)
                for m in re.findall(
                    r"https://bitbucket\.org/[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+/issues/(\d+)",
                    text,
                )
            ]
        )
        return list(set(issue_numbers))

    def fetch_repo_languages(self) -> Optional[Dict[str, int]]:
        try:
            url = f"{self.base_url}/repositories/{self.owner}/{self.repo_name}"

            def _make_request():
                response = requests.get(url, headers=self.headers, timeout=30)
                response.raise_for_status()
                return response.json()

            repo_data = retry_api_call(_make_request)
            language = repo_data.get("language")
            return {language: 1} if language else None
        except Exception as e:
            logger.debug(f"Failed to fetch repository language from Bitbucket API: {e}")
            return None

    def fetch_issue_count(self) -> dict:
        try:
            base = f"{self.base_url}/repositories/{self.owner}/{self.repo_name}/issues"

            def _count(state_query: str) -> int:
                def _make_request():
                    response = requests.get(
                        base,
                        headers=self.headers,
                        params={"q": state_query, "pagelen": 1},
                        timeout=30,
                    )
                    response.raise_for_status()
                    return response.json()

                data = retry_api_call(_make_request)
                return int(data.get("size", 0))

            open_count = _count('state="new" OR state="open"')
            closed_count = _count('state="resolved" OR state="closed"')
            return {
                "open": open_count,
                "closed": closed_count,
                "total": open_count + closed_count,
            }
        except Exception:
            return {"open": 0, "closed": 0, "total": 0}

    def fetch_patch(self, base_commit: str, head_commit: str) -> Optional[str]:
        try:
            url = f"{self.base_url}/repositories/{self.owner}/{self.repo_name}/diff/{base_commit}..{head_commit}"

            def _make_request():
                response = requests.get(url, headers=self.headers, timeout=30)
                response.raise_for_status()
                return response.text

            return retry_api_call(_make_request)
        except Exception:
            return None


class GitLabClient(PlatformClient):
    def __init__(
        self,
        owner: str,
        repo_name: str,
        token: Optional[str] = None,
        base_url: str = "https://gitlab.com",
    ):
        super().__init__(owner, repo_name, token)
        self.base_url = base_url.rstrip("/")
        self.api_url = f"{self.base_url}/api/v4"
        self.project_id = requests.utils.quote(self.repo_full_name, safe="")
        self.headers = {"Accept": "application/json"}
        if self.token:
            self.headers["PRIVATE-TOKEN"] = self.token

    def fetch_prs(
        self,
        cursor: Optional[str] = None,
        page_size: int = 50,
        start_date: Optional[datetime] = None,
    ) -> dict:
        current_page_size = page_size
        params = {
            "state": "merged",
            "order_by": "created_at",
            "sort": "desc",
        }
        if cursor:
            params["page"] = cursor
        if start_date:
            params["created_after"] = start_date.isoformat()

        url = f"{self.api_url}/projects/{self.project_id}/merge_requests"

        while True:
            params["per_page"] = current_page_size

            def _make_request():
                response = requests.get(
                    url, headers=self.headers, params=params, timeout=30
                )
                response.raise_for_status()
                return response

            try:
                response = retry_api_call(_make_request)
                break
            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 504:
                    if current_page_size > MIN_PAGE_SIZE:
                        current_page_size = current_page_size // 2
                        logger.warning(
                            f"504 Gateway Timeout with page_size={current_page_size * 2}, "
                            f"retrying with page_size={current_page_size}"
                        )
                        continue
                    logger.warning(
                        f"504 Gateway Timeout at minimum page_size={current_page_size}, "
                        f"giving up page-size backoff"
                    )
                raise

        data = response.json()
        next_page = response.headers.get("X-Next-Page", "")

        pr_nodes = []
        for mr in data:
            mr_iid = mr.get("iid")
            files = self._fetch_mr_changes(mr_iid)
            mr_details = self._fetch_mr_details(mr_iid)

            linked_issues = []
            body = mr.get("description", "") or ""
            issue_numbers = self.extract_issue_number_from_text(body)
            issue_numbers.extend(self._fetch_closing_issues(mr_iid))
            for issue_num in set(issue_numbers):
                issue_data = self.fetch_issue(issue_num)
                if issue_data:
                    linked_issues.append(issue_data)

            author = mr.get("author", {}) or {}
            author_login = author.get("username", "") or ""
            diff_refs = mr.get("diff_refs") or mr_details.get("diff_refs") or {}

            pr_nodes.append(
                {
                    "number": mr_iid,
                    "title": mr.get("title", ""),
                    "body": body,
                    "baseRefOid": diff_refs.get("base_sha", ""),
                    "headRefOid": mr.get("sha", "")
                    or mr_details.get("sha", "")
                    or diff_refs.get("head_sha", ""),
                    "baseRefName": mr.get("target_branch", ""),
                    "headRefName": mr.get("source_branch", ""),
                    "mergedAt": mr.get("merged_at", ""),
                    "createdAt": mr.get("created_at", ""),
                    "url": mr.get("web_url", ""),
                    "author": {
                        "login": author_login,
                        "isBot": _is_bot_username(author_login),
                        "__typename": "User",
                    },
                    "files": {"nodes": files},
                    "closingIssuesReferences": {"nodes": linked_issues},
                    "labels": {
                        "nodes": [{"name": label} for label in (mr.get("labels") or [])]
                    },
                }
            )

        primary_language_name = None
        try:
            languages = self.fetch_repo_languages()
            if languages:
                primary_language_name = max(languages, key=languages.get)
        except Exception:
            pass

        return {
            "data": {
                "repository": {
                    "primaryLanguage": {"name": primary_language_name},
                    "owner": {"login": self.owner},
                    "name": self.repo_name,
                    "pullRequests": {
                        "pageInfo": {
                            "hasNextPage": bool(next_page),
                            "endCursor": next_page or None,
                        },
                        "nodes": pr_nodes,
                    },
                }
            }
        }

    def _fetch_mr_details(self, mr_iid: int) -> dict:
        try:
            url = f"{self.api_url}/projects/{self.project_id}/merge_requests/{mr_iid}"

            def _make_request():
                response = requests.get(url, headers=self.headers, timeout=30)
                response.raise_for_status()
                return response.json()

            return retry_api_call(_make_request) or {}
        except Exception:
            return {}

    def _fetch_mr_changes(self, mr_iid: int) -> List[dict]:
        try:
            url = f"{self.api_url}/projects/{self.project_id}/merge_requests/{mr_iid}/changes"

            def _make_request():
                response = requests.get(url, headers=self.headers, timeout=30)
                response.raise_for_status()
                return response.json()

            mr_data = retry_api_call(_make_request)
            files = []
            for change in mr_data.get("changes", []):
                diff_text = change.get("diff", "")
                additions = sum(
                    1
                    for line in diff_text.split("\n")
                    if line.startswith("+") and not line.startswith("+++")
                )
                deletions = sum(
                    1
                    for line in diff_text.split("\n")
                    if line.startswith("-") and not line.startswith("---")
                )
                if change.get("new_file"):
                    change_type = "ADDED"
                elif change.get("deleted_file"):
                    change_type = "DELETED"
                elif change.get("renamed_file"):
                    change_type = "RENAMED"
                else:
                    change_type = "MODIFIED"
                files.append(
                    {
                        "path": change.get("new_path") or change.get("old_path", ""),
                        "changeType": change_type,
                        "additions": additions,
                        "deletions": deletions,
                    }
                )
            return files
        except Exception:
            return []

    def _fetch_closing_issues(self, mr_iid: int) -> List[int]:
        try:
            url = f"{self.api_url}/projects/{self.project_id}/merge_requests/{mr_iid}/closes_issues"

            def _make_request():
                response = requests.get(url, headers=self.headers, timeout=30)
                response.raise_for_status()
                return response.json()

            issues = retry_api_call(_make_request)
            return [issue.get("iid") for issue in issues if issue.get("iid")]
        except Exception:
            return []

    def fetch_issue(self, issue_number: int) -> Optional[dict]:
        try:
            url = f"{self.api_url}/projects/{self.project_id}/issues/{issue_number}"

            def _make_request():
                response = requests.get(url, headers=self.headers, timeout=30)
                response.raise_for_status()
                return response.json()

            issue = retry_api_call(_make_request)
            return {
                "number": issue.get("iid"),
                "title": issue.get("title", ""),
                "body": issue.get("description", "") or "",
                "state": "CLOSED"
                if issue.get("state") == "closed"
                else issue.get("state", "").upper(),
                "__typename": "Issue",
            }
        except Exception:
            return None

    def get_repo_url(self, include_token: bool = False) -> str:
        host = self.base_url.replace("https://", "").replace("http://", "")
        if include_token and self.token:
            return f"https://oauth2:{self.token}@{host}/{self.repo_full_name}.git"
        return f"{self.base_url}/{self.repo_full_name}.git"

    def extract_issue_number_from_text(self, text: str) -> List[int]:
        if not text:
            return []
        issue_numbers = []
        issue_numbers.extend([int(m) for m in re.findall(r"(?<!\!)#(\d+)", text)])
        issue_numbers.extend(
            [int(m) for m in re.findall(r"https?://[^/\s]+/.+?/-/issues/(\d+)", text)]
        )
        issue_numbers.extend(
            [
                int(m)
                for m in re.findall(
                    r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)",
                    text,
                    flags=re.IGNORECASE,
                )
            ]
        )
        return list(set(issue_numbers))

    def fetch_repo_languages(self) -> Optional[Dict[str, int]]:
        try:
            url = f"{self.api_url}/projects/{self.project_id}/languages"

            def _make_request():
                response = requests.get(url, headers=self.headers, timeout=30)
                response.raise_for_status()
                return response.json()

            languages = retry_api_call(_make_request)
            if not languages:
                return None
            return {
                lang: int(float(weight) * 100) for lang, weight in languages.items()
            }
        except Exception as e:
            logger.debug(f"Failed to fetch repository languages from GitLab API: {e}")
            return None

    def fetch_issue_count(self) -> dict:
        try:
            base_url = f"{self.api_url}/projects/{self.project_id}/issues"

            def _count(state: str) -> int:
                def _make_request():
                    response = requests.get(
                        base_url,
                        headers=self.headers,
                        params={"state": state, "per_page": 1},
                        timeout=30,
                    )
                    response.raise_for_status()
                    return response

                response = retry_api_call(_make_request)
                total_header = response.headers.get("X-Total")
                if total_header is not None:
                    try:
                        return int(total_header)
                    except ValueError:
                        pass
                return len(response.json() or [])

            open_count = _count("opened")
            closed_count = _count("closed")
            return {
                "open": open_count,
                "closed": closed_count,
                "total": open_count + closed_count,
            }
        except Exception:
            return {"open": 0, "closed": 0, "total": 0}

    def fetch_patch(self, base_commit: str, head_commit: str) -> Optional[str]:
        try:
            url = f"{self.api_url}/projects/{self.project_id}/repository/compare"

            def _make_request():
                response = requests.get(
                    url,
                    headers=self.headers,
                    params={"from": base_commit, "to": head_commit},
                    timeout=30,
                )
                response.raise_for_status()
                return response.json()

            data = retry_api_call(_make_request)
            diffs = data.get("diffs", []) or []
            if not diffs:
                return None

            chunks = []
            for item in diffs:
                diff_text = item.get("diff", "")
                if diff_text:
                    old_path = item.get("old_path") or item.get("new_path") or ""
                    new_path = item.get("new_path") or item.get("old_path") or ""

                    if item.get("new_file"):
                        old_marker = "/dev/null"
                        new_marker = f"b/{new_path}"
                    elif item.get("deleted_file"):
                        old_marker = f"a/{old_path}"
                        new_marker = "/dev/null"
                    else:
                        old_marker = f"a/{old_path}"
                        new_marker = f"b/{new_path}"

                    header = (
                        f"diff --git a/{old_path} b/{new_path}\n"
                        f"--- {old_marker}\n"
                        f"+++ {new_marker}\n"
                    )
                    chunks.append(header + diff_text)
            return "\n".join(chunks) if chunks else None
        except Exception:
            return None
