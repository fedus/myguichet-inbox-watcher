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
    ConfigurationError,
    get_source_account,
    get_source_accounts,
    load_environment,
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
    raise LoginError("Timed out waiting for LuxTrust device approval.")


def start_luxtrust_login(page: Page, username: str, password: str) -> None:
    """Fill the first-factor form; LuxTrust device approval remains external."""
    try:
        page.get_by_role("link", name=re.compile(r"LuxTrust")).click()
        luxtrust = page.frame_locator('iframe[title^="Connection to LuxTrust"]')
        luxtrust.get_by_text("LuxTrust Mobile", exact=True).click()
        luxtrust.get_by_role("textbox", name="User ID").fill(username)
        luxtrust.get_by_role("textbox", name="Password").fill(password)
        luxtrust.get_by_role("button", name="Next").click()
    except PlaywrightError as error:
        raise LoginError(
            "Could not find the expected LuxTrust login controls. "
            "The portal page may have changed; run with the account's HEADLESS setting disabled."
        ) from error


def refresh_cookie(account: MyGuichetAccountConfig | None = None) -> str:
    """Log in if needed, write cookie.txt atomically, and return its value."""
    load_environment()
    if account is None:
        accounts = get_source_accounts()
        if len(accounts) != 1:
            raise LoginError("Use --account when more than one account is configured.")
        account = myguichet_account_from_source(accounts[0])
    url = portal_url(account.language)
    prepare_private_directory(account.runtime_dir)
    prepare_private_directory(account.profile_dir)

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
        except PlaywrightError as error:
            raise LoginError(
                "The browser could not complete the MyGuichet login flow."
            ) from error
        finally:
            context.close()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Refresh a MyGuichet session cookie through LuxTrust."
    )
    parser.add_argument(
        "--account",
        help="Configured account to refresh. Required when multiple accounts exist.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for manually refreshing cookie.txt."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        load_environment()
        if args.account:
            account = myguichet_account_from_source(get_source_account(args.account))
        else:
            accounts = get_source_accounts()
            if len(accounts) != 1:
                raise LoginError(
                    "Use --account when more than one account is configured."
                )
            account = myguichet_account_from_source(accounts[0])
        with exclusive_lock(account.lock_file):
            refresh_cookie(account)
    except AlreadyRunning as error:
        print(str(error), file=sys.stderr)
        return 1
    except (ConfigurationError, LoginError, OSError, RuntimeError) as error:
        print(f"Login failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
