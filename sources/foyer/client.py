"""HTTP client for Foyer customer documents."""

from __future__ import annotations

import base64
import hashlib
import secrets
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


AUTH_BASE_URL = "https://ssowebauth.lefoyer.lu"
API_BASE_URL = "https://api.foyer.lu"
APP_BASE_URL = "https://dj.foyer.lu"
CLIENT_ID = "DIGITALJOURNEY_PKCE"
REDIRECT_URI = f"{APP_BASE_URL}/"
SCOPE = "openid profile email offline_access"
REQUEST_TIMEOUT_SECONDS = (10, 60)


class FoyerError(RuntimeError):
    """Base class for expected Foyer errors."""


class FoyerAuthenticationError(FoyerError):
    """Foyer rejected credentials or the login flow changed."""


class FoyerRequestError(FoyerError):
    """A Foyer request failed."""


class FoyerResponseError(FoyerError):
    """Foyer returned data that does not match the expected shape."""


class FoyerSessionExpired(FoyerError):
    """The saved Foyer token is no longer accepted."""


class _InputParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.inputs: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "input":
            return
        item = {key: value or "" for key, value in attrs}
        name = item.get("name")
        if name:
            self.inputs[name] = item.get("value", "")


def _json(response: requests.Response, description: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as error:
        raise FoyerResponseError(
            f"Foyer returned invalid JSON for {description}."
        ) from error
    finally:
        response.close()
    if not isinstance(payload, dict):
        raise FoyerResponseError(
            f"Foyer returned an unexpected response for {description}."
        )
    return payload


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _extract_form_inputs(html: str) -> dict[str, str]:
    parser = _InputParser()
    parser.feed(html)
    return parser.inputs


def _authorization_code_from_location(location: str, expected_state: str) -> str | None:
    parsed = urlparse(location)
    if parsed.netloc != "dj.foyer.lu":
        return None
    values = parse_qs(parsed.query)
    if values.get("state", [""])[0] != expected_state:
        raise FoyerAuthenticationError("Foyer login returned an unexpected OIDC state.")
    code = values.get("code", [""])[0]
    return code or None


def _looks_like_rejected_credentials(text: str) -> bool:
    """Detect common login-form errors without depending on exact markup."""
    normalized = " ".join(text.casefold().split())
    return any(
        marker in normalized
        for marker in (
            "incorrect",
            "mot de passe",
            "identifiant",
            "authentication failed",
            "invalid",
        )
    )


class FoyerClient:
    """Perform authenticated requests against the Foyer customer API."""

    def __init__(self) -> None:
        self.session = requests.Session()
        retries = Retry(
            total=3,
            connect=3,
            read=3,
            status=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST", "PUT"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retries)
        self.session.mount("https://", adapter)
        self.session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "fr,en;q=0.8",
                "Origin": APP_BASE_URL,
                "Referer": f"{APP_BASE_URL}/",
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
            }
        )

    def set_access_token(self, access_token: str) -> None:
        """Attach a bearer token to subsequent API requests."""
        self.session.headers["Authorization"] = f"Bearer {access_token}"

    def login(self, username: str, password: str) -> dict[str, Any]:
        """Authenticate with username/password using the browser PKCE flow."""
        state = secrets.token_urlsafe(48)
        code_verifier = secrets.token_urlsafe(64)
        authorize_params = {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "state": state,
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPE,
            "code_challenge": _pkce_challenge(code_verifier),
            "code_challenge_method": "S256",
            "nonce": state,
        }
        authorize_path = f"/form/oidc/authorize?{urlencode(authorize_params)}"
        try:
            response = self.session.get(
                f"{AUTH_BASE_URL}{authorize_path}",
                headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as error:
            raise FoyerRequestError(f"Could not open the Foyer login page: {error}") from error
        response_text = response.text
        response.close()

        fields = _extract_form_inputs(response_text)
        lpf_token = fields.get("lpfAuthTokenId", "")
        uri = fields.get("uri") or authorize_path
        if not lpf_token:
            raise FoyerAuthenticationError(
                "Foyer login page did not contain the expected login form token."
            )

        form = {
            "testActionForm": fields.get("testActionForm", "NONE"),
            "authtype": fields.get("authtype", "0"),
            "user": username,
            "password": password,
            "rememberMe": "on",
            "lpfFragmentUrl": fields.get("lpfFragmentUrl", ""),
            "lpfAuthTokenId": lpf_token,
            "uri": uri,
            "ident": fields.get("ident", "-1"),
            "msgstate": fields.get("msgstate", "off"),
            "ok": "Ok",
        }
        try:
            response = self.session.post(
                f"{AUTH_BASE_URL}/pxpadmin/bin/authform.cgi",
                data=form,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": AUTH_BASE_URL,
                    "Referer": f"{AUTH_BASE_URL}{authorize_path}",
                },
                allow_redirects=False,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as error:
            raise FoyerRequestError(f"Could not submit Foyer credentials: {error}") from error

        code = self._follow_login_redirects(response, state)
        return self.exchange_code(code, code_verifier)

    def _follow_login_redirects(
        self, response: requests.Response, expected_state: str
    ) -> str:
        try:
            current = response
            for _ in range(8):
                location = current.headers.get("Location", "")
                status_code = current.status_code
                response_text = current.text if not location else ""
                current.close()
                if not location:
                    if status_code in (400, 401, 403):
                        raise FoyerAuthenticationError(
                            f"Foyer rejected the configured credentials (HTTP {status_code})."
                        )
                    if _looks_like_rejected_credentials(response_text):
                        raise FoyerAuthenticationError(
                            "Foyer rejected the configured username/password."
                        )
                    raise FoyerAuthenticationError(
                        "Foyer login did not redirect to an authorization code."
                    )
                code = _authorization_code_from_location(location, expected_state)
                if code:
                    return code
                if location.startswith("/"):
                    location = f"{AUTH_BASE_URL}{location}"
                try:
                    current = self.session.get(
                        location,
                        headers={
                            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
                        },
                        allow_redirects=False,
                        timeout=REQUEST_TIMEOUT_SECONDS,
                    )
                except requests.RequestException as error:
                    raise FoyerRequestError(
                        f"Could not follow Foyer login redirect: {error}"
                    ) from error
            raise FoyerAuthenticationError("Foyer login redirected too many times.")
        finally:
            response.close()

    def _post_token(self, data: dict[str, str], description: str) -> dict[str, Any]:
        try:
            response = self.session.post(
                f"{AUTH_BASE_URL}/form/oidc/token",
                data=data,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": APP_BASE_URL,
                    "Referer": f"{APP_BASE_URL}/",
                },
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as error:
            raise FoyerRequestError(f"Could not contact the Foyer token service: {error}") from error
        if response.status_code != 200:
            status_code = response.status_code
            response.close()
            if status_code in (400, 401, 403):
                raise FoyerSessionExpired(
                    f"Foyer rejected the token request for {description} (HTTP {status_code})."
                )
            raise FoyerRequestError(
                f"Foyer token request for {description} failed with HTTP {status_code}."
            )
        payload = _json(response, description)
        if not isinstance(payload.get("access_token"), str):
            raise FoyerAuthenticationError(
                f"Foyer did not return an access token for {description}."
            )
        return payload

    def exchange_code(self, code: str, code_verifier: str) -> dict[str, Any]:
        """Exchange an OIDC authorization code for access/refresh tokens."""
        return self._post_token(
            {
                "client_id": CLIENT_ID,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": code_verifier,
            },
            "authorization code",
        )

    def refresh_access_token(self, refresh_token: str) -> dict[str, Any]:
        """Use a saved refresh token to obtain a fresh access token."""
        return self._post_token(
            {
                "client_id": CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            "refresh token",
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        try:
            response = self.session.request(
                method,
                f"{API_BASE_URL}{path}",
                timeout=REQUEST_TIMEOUT_SECONDS,
                **kwargs,
            )
        except requests.RequestException as error:
            raise FoyerRequestError(f"Could not retrieve {path}: {error}") from error
        if response.status_code in (401, 403):
            response.close()
            raise FoyerSessionExpired(
                f"Foyer session was rejected while requesting {path} "
                f"(HTTP {response.status_code})."
            )
        if response.status_code >= 400:
            status_code = response.status_code
            response.close()
            raise FoyerRequestError(
                f"Foyer request {path} failed with HTTP {status_code}."
            )
        return response

    def _get_json(self, path: str, **params: str | int) -> dict[str, Any]:
        response = self._request("GET", path, params=params)
        return _json(response, path)

    def user_profile(self) -> dict[str, Any]:
        """Return the logged-in customer profile."""
        return self._get_json("/v1/user-profile/user-profile")

    def list_json(self, path: str, params: dict[str, str | int]) -> dict[str, Any]:
        """Return one JSON document listing page."""
        response = self._request("GET", path, params=params)
        return _json(response, path)

    def download_url(self, url: str) -> requests.Response:
        """Return a streaming document response for a Foyer download URL."""
        parsed = urlparse(url)
        if parsed.netloc != "api.foyer.lu" or parsed.path != "/files/download":
            raise FoyerResponseError("Foyer document download URL has an unexpected host/path.")
        return self._request("GET", f"{parsed.path}?{parsed.query}", stream=True)

    def close(self) -> None:
        """Close pooled HTTP connections."""
        self.session.close()
