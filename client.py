"""Small read-only client for the MyGuichet inbox API."""

from __future__ import annotations

from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


API_BASE_URL = "https://www.services-publics.lu/fpgun-iep-api/api"
REQUEST_TIMEOUT_SECONDS = (10, 60)


class MyGuichetError(RuntimeError):
    """Base class for expected portal/API errors."""


class SessionExpired(MyGuichetError):
    """The saved browser session is no longer accepted by the API."""


class PortalRequestError(MyGuichetError):
    """A request failed for a reason other than an expired session."""


class PortalResponseError(MyGuichetError):
    """The portal returned a response that does not match the expected API shape."""


class MyGuichetClient:
    """Perform authenticated, read-only requests against the inbox endpoints."""

    def __init__(self, cookie_header: str, space_id: str, language: str) -> None:
        self.space_id = space_id
        self.language = language
        self.session = requests.Session()
        retries = Retry(
            total=3,
            connect=3,
            read=3,
            status=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retries))
        self.session.headers.update(
            {
                "ctie-tam-api": "true",
                "Accept": "*/*",
                "Referer": "https://www.services-publics.lu/",
                # Match a normal Chromium request; some identity providers
                # reject uncommon HTTP-client user agents before authentication.
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                "Cookie": cookie_header,
            }
        )

    def _get(
        self, path: str, *, stream: bool = False, **params: str | int
    ) -> requests.Response:
        """Return one successful API response or raise a clear, safe exception."""
        try:
            response = self.session.get(
                f"{API_BASE_URL}{path}",
                params=params,
                allow_redirects=False,
                timeout=REQUEST_TIMEOUT_SECONDS,
                stream=stream,
            )
        except requests.RequestException as error:
            raise PortalRequestError(f"Could not retrieve {path}: {error}") from error

        if 300 <= response.status_code < 400 or response.status_code in (401, 403):
            response.close()
            raise SessionExpired(
                f"Session was rejected while requesting {path} (HTTP {response.status_code})."
            )
        if response.status_code >= 400:
            status_code = response.status_code
            response.close()
            raise PortalRequestError(
                f"Portal request {path} failed with HTTP {status_code}."
            )

        content_type = response.headers.get("Content-Type", "").lower()
        if "text/html" in content_type:
            response.close()
            raise SessionExpired(f"Portal redirected {path} to an HTML login page.")
        return response

    @staticmethod
    def _json(response: requests.Response, path: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as error:
            raise PortalResponseError(
                f"Portal returned invalid JSON for {path}."
            ) from error
        finally:
            response.close()
        if not isinstance(payload, dict):
            raise PortalResponseError(
                f"Portal returned an unexpected JSON response for {path}."
            )
        return payload

    def list_communications(self, page: int, per_page: int) -> dict[str, Any]:
        """Return one page of inbox communication metadata."""
        path = "/communications/v1"
        response = self._get(
            path,
            spaceId=self.space_id,
            language=self.language,
            currentPage=page,
            requestsPerPage=per_page,
            sortedColumn="sentDate",
        )
        return self._json(response, path)

    def get_edelivery(self, communication_id: str) -> dict[str, Any]:
        """Return one eDelivery communication and its attachment metadata."""
        path = f"/communications/v1/edelivery/{communication_id}"
        response = self._get(path, language=self.language, spaceId=self.space_id)
        return self._json(response, path)

    def download_document(self, document_id: str, filename: str) -> requests.Response:
        """Return a streaming document response; the caller must close it."""
        return self._get(
            f"/documents/v1/download/{document_id}",
            stream=True,
            spaceId=self.space_id,
            filename=filename,
            isExternal="true",
        )

    def close(self) -> None:
        """Close pooled HTTP connections when a polling pass finishes."""
        self.session.close()
