"""Small Jira REST client used by the resumable transition workflow."""

import base64
import json
import os
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from dotenv import load_dotenv


class JiraConfigurationError(RuntimeError):
    pass


class JiraApiError(RuntimeError):
    def __init__(self, message: str, status_code: Optional[int] = None, retryable: bool = True):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class JiraClient:
    def __init__(self, base_url: str, email: str, api_token: str, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.api_token = api_token
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> "JiraClient":
        load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
        base_url = os.environ.get("JIRA_BASE_URL")
        email = os.environ.get("JIRA_EMAIL")
        api_token = os.environ.get("JIRA_API_TOKEN")
        missing = [
            name
            for name, value in (
                ("JIRA_BASE_URL", base_url),
                ("JIRA_EMAIL", email),
                ("JIRA_API_TOKEN", api_token),
            )
            if not value
        ]
        if missing:
            raise JiraConfigurationError(
                f"Missing Jira configuration: {', '.join(missing)}."
            )
        api_version = os.environ.get("JIRA_API_VERSION", "3")
        normalized_base = base_url.rstrip("/")
        if "/rest/api/" not in normalized_base:
            normalized_base = f"{normalized_base}/rest/api/{api_version}"
        return cls(normalized_base, email, api_token)

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        auth = base64.b64encode(f"{self.email}:{self.api_token}".encode()).decode()
        headers = {
            "Accept": "application/json",
            "Authorization": f"Basic {auth}",
        }
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()

        request = Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            raw = exc.read().decode(errors="replace")
            detail = self._error_detail(raw)
            retryable = exc.code in {408, 409, 429} or exc.code >= 500
            raise JiraApiError(
                f"Jira API returned HTTP {exc.code}: {detail}",
                status_code=exc.code,
                retryable=retryable,
            ) from exc
        except (TimeoutError, URLError, OSError) as exc:
            reason = getattr(exc, "reason", None) or str(exc)
            raise JiraApiError(f"Could not reach Jira: {reason}", retryable=True) from exc

        if not raw:
            return {}
        try:
            return json.loads(raw.decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise JiraApiError("Jira returned an invalid JSON response.", retryable=False) from exc

    @staticmethod
    def _error_detail(raw: str) -> str:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return raw[:300] or "No error details provided."
        messages = payload.get("errorMessages") or []
        if messages:
            return "; ".join(str(message) for message in messages)
        return str(payload.get("message") or payload)[:300]

    def get_issue(self, issue_key: str) -> dict:
        encoded_key = quote(issue_key, safe="")
        return self._request(
            "GET",
            f"/issue/{encoded_key}?fields=summary,status",
        )

    def get_transitions(self, issue_key: str) -> list[dict]:
        encoded_key = quote(issue_key, safe="")
        payload = self._request("GET", f"/issue/{encoded_key}/transitions")
        return payload.get("transitions", [])

    def search_issues(self, jql: str = "project = SCRUM order by created DESC", max_results: int = 10) -> list[dict]:
        # Jira Cloud requires bounded JQL; ensure a project clause exists if empty or unbounded
        trimmed_jql = jql.strip()
        if not trimmed_jql or trimmed_jql.lower().startswith("order by"):
            trimmed_jql = f"project = SCRUM {trimmed_jql}".strip()
        encoded_jql = quote(trimmed_jql, safe="")
        try:
            payload = self._request("GET", f"/search/jql?jql={encoded_jql}&maxResults={max_results}&fields=summary,status,issuetype")
        except JiraApiError as exc:
            if exc.status_code == 404:
                payload = self._request("GET", f"/search?jql={encoded_jql}&maxResults={max_results}&fields=summary,status,issuetype")
            else:
                raise
        return payload.get("issues", [])

    def transition_issue(self, issue_key: str, transition_id: str) -> None:
        encoded_key = quote(issue_key, safe="")
        self._request(
            "POST",
            f"/issue/{encoded_key}/transitions",
            {"transition": {"id": str(transition_id)}},
        )