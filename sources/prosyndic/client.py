"""HTTP client for ProSyndic extranet documents."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


REQUEST_TIMEOUT_SECONDS = (10, 60)
AUTH_COOKIE_NAME = "X14"


class ProSyndicError(RuntimeError):
    """Base class for expected ProSyndic errors."""


class ProSyndicAuthenticationError(ProSyndicError):
    """The configured credentials were rejected or login changed."""


class ProSyndicRequestError(ProSyndicError):
    """A ProSyndic request failed."""


class ProSyndicResponseError(ProSyndicError):
    """ProSyndic returned data that does not match the expected shape."""


class ProSyndicSessionExpired(ProSyndicError):
    """The current ProSyndic session is no longer accepted."""


class ProSyndicClient:
    """Perform authenticated requests against one ProSyndic tenant."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        retries = Retry(
            total=3,
            connect=3,
            read=3,
            status=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retries)
        self.session.mount("https://", adapter)
        self.session.headers.update(
            {
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "fr,en;q=0.8",
                "Content-Type": "application/json; charset=utf-8",
                "Origin": self.base_url,
                "Referer": f"{self.base_url}/extranet",
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                "X-Requested-With": "XMLHttpRequest",
            }
        )

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    @staticmethod
    def _json(response: requests.Response, description: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as error:
            raise ProSyndicResponseError(
                f"ProSyndic returned invalid JSON for {description}."
            ) from error
        finally:
            response.close()
        if not isinstance(payload, dict):
            raise ProSyndicResponseError(
                f"ProSyndic returned an unexpected response for {description}."
            )
        return payload

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        try:
            response = self.session.request(
                method,
                self._url(path),
                timeout=REQUEST_TIMEOUT_SECONDS,
                **kwargs,
            )
        except requests.RequestException as error:
            raise ProSyndicRequestError(f"Could not retrieve {path}: {error}") from error

        if response.status_code in (401, 403) or response.url.endswith("/connexion/"):
            response.close()
            raise ProSyndicSessionExpired(
                f"ProSyndic session was rejected while requesting {path} "
                f"(HTTP {response.status_code})."
            )
        if response.status_code >= 400:
            status_code = response.status_code
            response.close()
            raise ProSyndicRequestError(
                f"ProSyndic request {path} failed with HTTP {status_code}."
            )
        return response

    def _get_json(self, path: str, **params: int) -> dict[str, Any]:
        response = self._request("GET", path, params=params)
        return self._json(response, path)

    def login(self, username: str, password: str) -> None:
        """Authenticate and attach the returned X14 token cookie to this session."""
        try:
            self.session.get(
                self._url("/connexion/"),
                timeout=REQUEST_TIMEOUT_SECONDS,
            ).close()
        except requests.RequestException as error:
            raise ProSyndicRequestError(
                f"Could not open the ProSyndic login page: {error}"
            ) from error

        body = {
            "login": username,
            "password": password,
            "password_confirmation_validation": False,
            "password_strenght_validation": False,
        }
        self.session.headers["Referer"] = f"{self.base_url}/connexion/"
        try:
            response = self.session.post(
                self._url("/authenticate"),
                json=body,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as error:
            raise ProSyndicRequestError(
                f"Could not submit ProSyndic credentials: {error}"
            ) from error
        if response.status_code >= 400:
            status_code = response.status_code
            response.close()
            raise ProSyndicAuthenticationError(
                f"ProSyndic rejected the configured credentials (HTTP {status_code})."
            )
        payload = self._json(response, "login")
        if payload.get("authentication") is not True:
            message = payload.get("authenticationResult") or "authentication failed"
            raise ProSyndicAuthenticationError(
                f"ProSyndic rejected the configured credentials: {message}"
            )

        token = payload.get("authenticationToken")
        if not isinstance(token, str) or not token:
            raise ProSyndicAuthenticationError(
                "ProSyndic login succeeded but did not return an authentication token."
            )
        self.session.cookies.set(
            AUTH_COOKIE_NAME,
            token,
            domain=self.base_url.removeprefix("https://").split("/", 1)[0],
            path="/",
        )

        try:
            optipol = self.session.post(
                self._url("/authenticateOptipol"),
                json=body,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException:
            optipol = None
        if optipol is not None:
            optipol.close()
        self.session.headers["Referer"] = f"{self.base_url}/extranet"

    def list_folder(self, folder_id: str | None, page: int, limit: int) -> dict[str, Any]:
        """Return one page of a document folder listing."""
        suffix = "" if folder_id is None else quote(folder_id, safe="")
        return self._get_json(
            f"/extranet/coproprietaire/documents/classeur/{suffix}",
            itemCountPerPage=limit,
            currentPageNumber=page,
        )

    def download_document(self, document_id: str) -> requests.Response:
        """Return a streaming document response; the caller must close it."""
        self.session.headers["Accept"] = (
            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        )
        try:
            return self._request(
                "GET",
                f"/extranet/documents/download/{quote(document_id, safe='')}",
                stream=True,
            )
        finally:
            self.session.headers["Accept"] = "application/json, text/javascript, */*; q=0.01"

    def close(self) -> None:
        """Close pooled HTTP connections."""
        self.session.close()
