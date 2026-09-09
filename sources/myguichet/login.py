"""Refresh the MyGuichet API session through LuxTrust and save its cookies."""

from __future__ import annotations

import argparse
import getpass
import re
import sys
import time
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

from config import (
    ConfigProvider,
    ConfigurationError,
    EnvConfigProvider,
)
from sources.myguichet.config import (
    MyGuichetAccountConfig,
    myguichet_account_from_source,
)
from storage import (
    AlreadyRunning,
    atomic_write_text,
    exclusive_lock,
    prepare_private_directory,
)


SSO_COOKIE_NAME = "LtpaToken2"
POLL_INTERVAL_SECONDS = 1.0
LUXTRUST_SUBMIT_ERROR_GRACE_SECONDS = 1.0
MAX_BROWSER_ERROR_CHARS = 3_500


class LoginError(RuntimeError):
    """The browser could not establish an authenticated MyGuichet session."""


def portal_url(language: str) -> str:
    """Return the normal public portal URL, not a hand-crafted login URL."""
    return f"https://www.services-publics.lu/fpgun-iep-front/?lang={language}"


def load_luxtrust_credentials(account: MyGuichetAccountConfig) -> tuple[str, str]:
    """Load credentials from .env or securely prompt in an interactive shell."""
    username = account.luxtrust_username
    password = account.luxtrust_password
    if (not username or not password) and not sys.stdin.isatty():
        raise LoginError(
            f"LuxTrust credentials are missing for account {account.name!r} and this "
            "run has no interactive terminal. Set the account's LuxTrust username "
            "and password in .env for unattended use."
        )
    if not username:
        username = input(f"LuxTrust User ID for {account.name}: ").strip()
    if not password:
        password = getpass.getpass(f"LuxTrust password for {account.name}: ")
    if not username or not password:
        raise LoginError("A LuxTrust User ID and password are required to log in.")
    return username, password


def cookie_applies_to_portal(cookie: dict[str, Any], language: str) -> bool:
    """Accept only cookies whose domain can actually serve the portal host."""
    portal_host = urlparse(portal_url(language)).hostname
    domain = str(cookie.get("domain", "")).lstrip(".").lower()
    return bool(
        portal_host
        and domain
        and (portal_host == domain or portal_host.endswith(f".{domain}"))
    )


def session_cookie_header(context: Any, language: str) -> str:
    """Build the cookie header consumed by the API client from the browser context."""
    cookies = [
        cookie
        for cookie in context.cookies()
        if cookie_applies_to_portal(cookie, language)
    ]
    if not any(cookie.get("name") == SSO_COOKIE_NAME for cookie in cookies):
        return ""
    return "; ".join(f"{cookie['name']}={cookie['value']}" for cookie in cookies)


def wait_for_session_cookie(context: Any, language: str, timeout_seconds: int) -> str:
    """Wait for LuxTrust approval to result in the API session cookie."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        cookie_header = session_cookie_header(context, language)
        if cookie_header:
            return cookie_header
        time.sleep(POLL_INTERVAL_SECONDS)
    raise LoginError(
        f"Timed out after {timeout_seconds}s waiting for LuxTrust approval to "
        "create a MyGuichet session cookie. Check whether the mobile request was "
        "approved, rejected, or expired."
    )


def _playwright_error_summary(error: PlaywrightError) -> str:
    text = str(error).strip()
    return text.splitlines()[0] if text else type(error).__name__


def _playwright_error_details(error: PlaywrightError) -> str:
    text = str(error).strip()
    if not text:
        return type(error).__name__
    if len(text) > MAX_BROWSER_ERROR_CHARS:
        return text[:MAX_BROWSER_ERROR_CHARS].rstrip() + " ..."
    return text


def _redact_text(text: str, *secrets: str) -> str:
    result = text
    for secret in secrets:
        if secret:
            result = result.replace(secret, "***")
    return result


def _visible_text(locator: Any, *secrets: str) -> str:
    try:
        text = locator.inner_text(timeout=2_000)
    except PlaywrightError:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 500:
        text = text[:500].rstrip() + " ..."
    return _redact_text(text, *secrets)


def _looks_like_rejected_credentials(text: str) -> bool:
    normalized = text.lower()
    return any(
        marker in normalized
        for marker in (
            "data you have entered are incorrect",
            "entered are incorrect",
            "incorrect. please try again",
            "données que vous avez saisies sont incorrectes",
            "daten sind nicht korrekt",
        )
    )


def _raise_if_luxtrust_rejected_credentials(
    luxtrust: Any, username: str, password: str
) -> None:
    visible = _visible_text(luxtrust.locator("body"), username, password)
    if _looks_like_rejected_credentials(visible):
        raise LoginError(
            "LuxTrust rejected the configured User ID/password before starting "
            "mobile approval. Check the account's LUXTRUST_USERNAME and "
            f"LUXTRUST_PASSWORD settings. Visible LuxTrust text: {visible}"
        )


def start_luxtrust_login(page: Page, username: str, password: str) -> None:
    """Fill the first-factor form; LuxTrust device approval remains external."""
    try:
        page.get_by_role("link", name=re.compile(r"LuxTrust")).click()
        luxtrust = page.frame_locator('iframe[title^="Connection to LuxTrust"]')
        luxtrust.get_by_text("LuxTrust Mobile", exact=True).click()
        luxtrust.get_by_role("textbox", name="User ID").fill(username)
        luxtrust.get_by_role("textbox", name="Password").fill(password)
        luxtrust.get_by_role("button", name="Next").click()
        time.sleep(LUXTRUST_SUBMIT_ERROR_GRACE_SECONDS)
        _raise_if_luxtrust_rejected_credentials(luxtrust, username, password)
    except PlaywrightError as error:
        raise LoginError(
            "Could not find the expected LuxTrust login controls. "
            "The portal page may have changed; run with the account's HEADLESS "
            f"setting disabled. Last browser error: {_playwright_error_summary(error)}"
        ) from error


def refresh_cookie(
    account: MyGuichetAccountConfig | None = None,
    config_provider: ConfigProvider | None = None,
) -> str:
    """Log in if needed, write cookie.txt atomically, and return its value."""
    provider = config_provider or EnvConfigProvider()
    provider.load()
    if account is None:
        accounts = provider.get_source_accounts()
        if len(accounts) != 1:
            raise LoginError("Use --account when more than one account is configured.")
        account = myguichet_account_from_source(accounts[0])
    url = portal_url(account.language)
    prepare_private_directory(account.runtime_dir)
    prepare_private_directory(account.profile_dir)

    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(account.profile_dir), headless=account.headless
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                print(
                    f"[{account.name}] Opening MyGuichet "
                    f"({'headless' if account.headless else 'visible'} browser) ..."
                )
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)

                cookie_header = ""
                if "fpgun-iep-front" in page.url and "TAMLoginServlet" not in page.url:
                    cookie_header = session_cookie_header(context, account.language)
                if not cookie_header:
                    username, password = load_luxtrust_credentials(account)
                    start_luxtrust_login(page, username, password)
                    print(
                        f"[{account.name}] LuxTrust credentials submitted. "
                        "Approve the request on your device."
                    )
                    cookie_header = wait_for_session_cookie(
                        context, account.language, account.login_timeout_seconds
                    )

                if not cookie_header:
                    raise LoginError(
                        "The browser did not expose an authenticated MyGuichet session cookie."
                    )
                atomic_write_text(account.cookie_file, cookie_header + "\n")
                print(f"[{account.name}] Authenticated session saved to cookie.txt.")
                return cookie_header
            finally:
                context.close()
    except PlaywrightError as error:
        raise LoginError(
            "The browser could not complete the MyGuichet login flow. "
            f"Last browser error:\n{_playwright_error_details(error)}"
        ) from error


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Refresh a MyGuichet session cookie through LuxTrust."
    )
    parser.add_argument(
        "--account",
        help="Configured account to refresh. Required when multiple accounts exist.",
    )
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None, config_provider: ConfigProvider | None = None
) -> int:
    """CLI entry point for manually refreshing cookie.txt."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    provider = config_provider or EnvConfigProvider()
    try:
        provider.load()
        if args.account:
            account = myguichet_account_from_source(
                provider.get_source_account(args.account)
            )
        else:
            accounts = provider.get_source_accounts()
            if len(accounts) != 1:
                raise LoginError(
                    "Use --account when more than one account is configured."
                )
            account = myguichet_account_from_source(accounts[0])
        with exclusive_lock(account.lock_file):
            refresh_cookie(account, provider)
    except AlreadyRunning as error:
        print(str(error), file=sys.stderr)
        return 1
    except (ConfigurationError, LoginError, OSError, RuntimeError) as error:
        print(f"Login failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
