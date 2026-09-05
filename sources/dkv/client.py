"""HTTP client for DKV/Lalux EasyApp reimbursements."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


AUTH_BASE_URL = "https://auth.lalux-partners.lu/auth/realms/ClientExternalRealm"
SECURE_API_BASE_URL = "https://api-client-external-secure.lalux-partners.lu"
TOKEN_PATH = "/protocol/openid-connect/token"
CLIENT_ID = "easyapphome-client"
APP_SOURCE = "EASY_APP_HOME"
REQUEST_TIMEOUT_SECONDS = (10, 60)


class DkvError(RuntimeError):
    """Base class for expected DKV/Lalux EasyApp errors."""


class DkvAuthenticationError(DkvError):
    """The authentication flow did not return the expected data."""


class DkvRequestError(DkvError):
    """A DKV/Lalux EasyApp request failed."""


class DkvResponseError(DkvError):
    """The DKV/Lalux EasyApp API returned an unexpected response shape."""


class DkvSessionExpired(DkvError):
    """The saved access/refresh token is no longer accepted."""


class DkvClient:
    """Perform authenticated requests against the DKV/Lalux EasyApp API."""

    def __init__(self) -> None:
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
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en",
                "App-Source": APP_SOURCE,
                "Origin": "https://easyapphome.lalux.lu",
                "Referer": "https://easyapphome.lalux.lu/",
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
            }
        )

    def set_access_token(self, access_token: str) -> None:
        """Attach a bearer token to subsequent secure API requests."""
        self.session.headers["Authorization"] = f"Bearer {access_token}"

    def _post_token(self, data: dict[str, str]) -> requests.Response:
        try:
            return self.session.post(
                f"{AUTH_BASE_URL}{TOKEN_PATH}",
                data=data,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as error:
            raise DkvRequestError(f"Could not contact the DKV auth service: {error}") from error

    @staticmethod
    def _json(response: requests.Response, description: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as error:
            raise DkvResponseError(
                f"DKV/Lalux EasyApp returned invalid JSON for {description}."
            ) from error
        finally:
            response.close()
        if not isinstance(payload, dict):
            raise DkvResponseError(
                f"DKV/Lalux EasyApp returned an unexpected response for {description}."
            )
        return payload

    @staticmethod
    def _require_token_payload(payload: dict[str, Any], description: str) -> dict[str, Any]:
        if not isinstance(payload.get("access_token"), str):
            raise DkvAuthenticationError(
                f"DKV/Lalux EasyApp did not return an access token for {description}."
            )
        if not isinstance(payload.get("refresh_token"), str):
            raise DkvAuthenticationError(
                f"DKV/Lalux EasyApp did not return a refresh token for {description}."
            )
        return payload

    def start_sms_login(
        self, username: str, password: str, otp_type: str
    ) -> str | dict[str, Any]:
        """Submit credentials and return a session token, or tokens if OTP was skipped."""
        response = self._post_token(
            {
                "username": username,
                "password": password,
                "otp_type": otp_type,
                "client_id": CLIENT_ID,
                "grant_type": "password",
            }
        )
        if response.status_code not in (200, 202):
            status_code = response.status_code
            response.close()
            if status_code in (400, 401, 403):
                raise DkvAuthenticationError(
                    f"DKV/Lalux EasyApp rejected the configured credentials (HTTP {status_code})."
                )
            raise DkvRequestError(
                f"DKV/Lalux EasyApp login failed with HTTP {status_code}."
            )
        payload = self._json(response, "password login")
        if response.status_code == 202:
            session_token = payload.get("session_token")
            if not isinstance(session_token, str) or not session_token:
                raise DkvAuthenticationError(
                    "DKV/Lalux EasyApp requested OTP but did not return a session token."
                )
            return session_token
        if response.status_code == 200:
            return self._require_token_payload(payload, "password login")
        raise DkvRequestError("DKV/Lalux EasyApp login returned an unexpected status.")

    def complete_sms_login(
        self, session_token: str, otp: str, otp_type: str
    ) -> dict[str, Any]:
        """Exchange an externally supplied OTP for access/refresh tokens."""
        response = self._post_token(
            {
                "otp": otp,
                "session_code": session_token,
                "otp_type": otp_type,
                "client_id": CLIENT_ID,
                "grant_type": "password",
            }
        )
        if response.status_code != 200:
            status_code = response.status_code
            response.close()
            if status_code in (400, 401, 403):
                raise DkvAuthenticationError(
                    f"DKV/Lalux EasyApp rejected the supplied OTP (HTTP {status_code})."
                )
            raise DkvRequestError(
                f"DKV/Lalux EasyApp OTP login failed with HTTP {status_code}."
            )
        payload = self._json(response, "SMS OTP login")
        return self._require_token_payload(payload, "SMS OTP login")

    def refresh_access_token(self, refresh_token: str) -> dict[str, Any]:
        """Use a saved refresh token to obtain a fresh access token."""
        response = self._post_token(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CLIENT_ID,
            }
        )
        if response.status_code != 200:
            status_code = response.status_code
            response.close()
            if status_code in (400, 401, 403):
                raise DkvSessionExpired(
                    f"DKV/Lalux EasyApp rejected the saved refresh token (HTTP {status_code})."
                )
            raise DkvRequestError(
                f"DKV/Lalux EasyApp token refresh failed with HTTP {status_code}."
            )
        payload = self._json(response, "refresh token")
        return self._require_token_payload(payload, "refresh token")

    def _get(
        self, path: str, *, stream: bool = False, **params: str | int
    ) -> requests.Response:
        try:
            response = self.session.get(
                f"{SECURE_API_BASE_URL}{path}",
                params=params,
                allow_redirects=False,
                timeout=REQUEST_TIMEOUT_SECONDS,
                stream=stream,
            )
        except requests.RequestException as error:
            raise DkvRequestError(f"Could not retrieve {path}: {error}") from error

        if 300 <= response.status_code < 400 or response.status_code in (401, 403):
            response.close()
            raise DkvSessionExpired(
                f"DKV/Lalux EasyApp session was rejected while requesting {path} "
                f"(HTTP {response.status_code})."
            )
        if response.status_code >= 400:
            status_code = response.status_code
            response.close()
            raise DkvRequestError(
                f"DKV/Lalux EasyApp request {path} failed with HTTP {status_code}."
            )
        return response

    def _get_json(self, path: str, **params: str | int) -> dict[str, Any]:
        response = self._get(path, **params)
        return self._json(response, path)

    def list_refunds(self, page_index: int, limit: int) -> dict[str, Any]:
        """Return one infinite-scroll page of reimbursements."""
        return self._get_json("/refunds", limit=limit, pageIndex=page_index)

    def get_refund(self, refund_id: str) -> dict[str, Any]:
        """Return one reimbursement detail response."""
        return self._get_json(f"/refunds/{quote(refund_id, safe='')}")

    def download_document(self, document_id: str) -> requests.Response:
        """Return a streaming document response; the caller must close it."""
        return self._get(
            f"/documents/{quote(document_id, safe='')}",
            stream=True,
            idFile=document_id,
        )

    def close(self) -> None:
        """Close pooled HTTP connections."""
        self.session.close()
