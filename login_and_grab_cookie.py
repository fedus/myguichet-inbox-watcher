"""Refresh the MyGuichet API session through LuxTrust and save its cookies."""

from __future__ import annotations

import getpass
import os
import re
import sys
import time
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

from config import ROOT, get_bool, get_language, get_positive_int, load_environment
from storage import (
    AlreadyRunning,
    atomic_write_text,
    exclusive_lock,
    prepare_private_directory,
)


COOKIE_FILE = ROOT / "cookie.txt"
PROFILE_DIR = ROOT / ".browser-profile"
SSO_COOKIE_NAME = "LtpaToken2"
POLL_INTERVAL_SECONDS = 1.0


class LoginError(RuntimeError):
    """The browser could not establish an authenticated MyGuichet session."""


def portal_url(language: str) -> str:
    """Return the normal public portal URL, not a hand-crafted login URL."""
    return f"https://www.services-publics.lu/fpgun-iep-front/?lang={language}"


def load_luxtrust_credentials() -> tuple[str, str]:
    """Load credentials from .env or securely prompt in an interactive shell."""
    username = os.environ.get("LUXTRUST_USERNAME", "").strip()
    password = os.environ.get("LUXTRUST_PASSWORD", "")
    if (not username or not password) and not sys.stdin.isatty():
        raise LoginError(
            "LuxTrust credentials are missing and this run has no interactive terminal. "
            "Set LUXTRUST_USERNAME and LUXTRUST_PASSWORD in .env for unattended use."
        )
    if not username:
        username = input("LuxTrust User ID: ").strip()
    if not password:
        password = getpass.getpass("LuxTrust password: ")
    if not username or not password:
        raise LoginError("A LuxTrust User ID and password are required to log in.")
    return username, password


def cookie_applies_to_portal(cookie: dict[str, Any]) -> bool:
    """Accept only cookies whose domain can actually serve the portal host."""
    portal_host = urlparse(portal_url(get_language())).hostname
    domain = str(cookie.get("domain", "")).lstrip(".").lower()
    return bool(
        portal_host
        and domain
        and (portal_host == domain or portal_host.endswith(f".{domain}"))
    )


def session_cookie_header(context: Any) -> str:
    """Build the cookie header consumed by the API client from the browser context."""
    cookies = [
        cookie for cookie in context.cookies() if cookie_applies_to_portal(cookie)
    ]
    if not any(cookie.get("name") == SSO_COOKIE_NAME for cookie in cookies):
        return ""
    return "; ".join(f"{cookie['name']}={cookie['value']}" for cookie in cookies)


def wait_for_session_cookie(context: Any, timeout_seconds: int) -> str:
    """Wait for LuxTrust approval to result in the API session cookie."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        cookie_header = session_cookie_header(context)
        if cookie_header:
            return cookie_header
        time.sleep(POLL_INTERVAL_SECONDS)
    raise LoginError("Timed out waiting for LuxTrust device approval.")


def start_luxtrust_login(page: Page, username: str, password: str) -> None:
    """Fill the first-factor form; LuxTrust device approval remains external."""
    try:
        page.get_by_role("link", name=re.compile(r"LuxTrust")).click()
        # The session identifier in this title changes each time, so match only
        # its stable prefix.
        luxtrust = page.frame_locator('iframe[title^="Connection to LuxTrust"]')
        luxtrust.get_by_text("LuxTrust Mobile", exact=True).click()
        luxtrust.get_by_role("textbox", name="User ID").fill(username)
        luxtrust.get_by_role("textbox", name="Password").fill(password)
        luxtrust.get_by_role("button", name="Next").click()
    except PlaywrightError as error:
        raise LoginError(
            "Could not find the expected LuxTrust login controls. "
            "The portal page may have changed; run with MYGUICHET_HEADLESS=false to inspect it."
        ) from error


def refresh_cookie() -> str:
    """Log in if needed, write cookie.txt atomically, and return its value.

    This function deliberately does not take a process lock. The regular
    watcher owns that lock before calling it; direct execution is for manual
    troubleshooting and should not overlap a scheduled watcher run.
    """
    load_environment()
    language = get_language()
    headless = get_bool("MYGUICHET_HEADLESS", default=False)
    approval_timeout = get_positive_int("MYGUICHET_LOGIN_TIMEOUT_SECONDS", 300)
    url = portal_url(language)
    prepare_private_directory(PROFILE_DIR)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=headless
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            print(
                f"Opening MyGuichet ({'headless' if headless else 'visible'} browser) ..."
            )
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)

            # A still-valid persistent browser profile can refresh cookie.txt
            # without entering credentials or triggering another MFA request.
            cookie_header = ""
            if "fpgun-iep-front" in page.url and "TAMLoginServlet" not in page.url:
                cookie_header = session_cookie_header(context)
            if not cookie_header:
                username, password = load_luxtrust_credentials()
                start_luxtrust_login(page, username, password)
                print(
                    "LuxTrust credentials submitted. Approve the request on your device."
                )
                cookie_header = wait_for_session_cookie(context, approval_timeout)

            if not cookie_header:
                raise LoginError(
                    "The browser did not expose an authenticated MyGuichet session cookie."
                )
            atomic_write_text(COOKIE_FILE, cookie_header + "\n")
            print("Authenticated session saved to cookie.txt.")
            return cookie_header
        except PlaywrightError as error:
            raise LoginError(
                "The browser could not complete the MyGuichet login flow."
            ) from error
        finally:
            context.close()


def main() -> int:
    """CLI entry point for manually refreshing cookie.txt."""
    try:
        # The watcher calls refresh_cookie() while it already owns this lock.
        # Direct use of this script also needs the same protection.
        with exclusive_lock(ROOT / ".run.lock"):
            refresh_cookie()
    except AlreadyRunning as error:
        print(str(error), file=sys.stderr)
        return 1
    except (LoginError, OSError, RuntimeError) as error:
        print(f"Login failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
