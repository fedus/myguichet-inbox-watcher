"""Offline checks for the watcher helpers; no real portal account is used."""

from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch


WATCHER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WATCHER_ROOT))

import myguichet_get_new_messages as watcher  # noqa: E402
import mqtt_trigger  # noqa: E402
import config  # noqa: E402
from client import PortalResponseError  # noqa: E402
from config import AccountConfig  # noqa: E402


class FakeResponse:
    def __init__(
        self, chunks: list[bytes], content_type: str = "application/pdf"
    ) -> None:
        self.chunks = chunks
        self.headers = {"Content-Type": content_type}
        self.closed = False

    def iter_content(self, chunk_size: int):  # type: ignore[no-untyped-def]
        del chunk_size
        yield from self.chunks

    def close(self) -> None:
        self.closed = True


class FakeClient:
    def __init__(self, pages: dict[int, dict[str, object]]) -> None:
        self.pages = pages
        self.requested_pages: list[int] = []

    def list_communications(self, page: int, per_page: int) -> dict[str, object]:
        self.requested_pages.append(page)
        self.assert_requested_page_size(per_page)
        return self.pages[page]

    @staticmethod
    def assert_requested_page_size(per_page: int) -> None:
        if per_page != watcher.REQUESTS_PER_PAGE:
            raise AssertionError("Unexpected page size")


def communication(communication_id: int) -> dict[str, object]:
    return {"eDeliveryCommunicationHitDto": {"id": communication_id}}


def fake_account(root: Path) -> AccountConfig:
    return AccountConfig(
        name="alice",
        luxtrust_username="alice-user",
        luxtrust_password="secret",
        space_id="123",
        language="fr",
        headless=True,
        login_timeout_seconds=300,
        maximum_attachment_mb=100,
        download_dir=root / "downloads",
        runtime_dir=root,
        cookie_file=root / "cookie.txt",
        state_file=root / "state.json",
        profile_dir=root / ".browser-profile",
        lock_file=root / ".run.lock",
    )


class WatcherHelpersTest(unittest.TestCase):
    def test_safe_filename_removes_path_characters_and_controls(self) -> None:
        self.assertEqual(watcher.safe_filename(" ../a/b\\c\x00.pdf "), "_a_b_c_.pdf")

    def test_write_attachment_is_private_and_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "private" / "document.pdf"
            response = FakeResponse([b"part one", b"part two"])

            watcher.write_attachment(response, destination, maximum_bytes=1024)

            self.assertEqual(destination.read_bytes(), b"part onepart two")
            self.assertTrue(response.closed)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

    def test_empty_attachment_is_not_published(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "document.pdf"
            response = FakeResponse([])

            with self.assertRaises(PortalResponseError):
                watcher.write_attachment(response, destination, maximum_bytes=1024)

            self.assertFalse(destination.exists())
            self.assertTrue(response.closed)

    def test_oversized_attachment_is_closed_without_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "document.pdf"
            response = FakeResponse([b"small"])
            response.headers["Content-Length"] = "1000"

            with self.assertRaises(PortalResponseError):
                watcher.write_attachment(response, destination, maximum_bytes=100)

            self.assertFalse(destination.exists())
            self.assertTrue(response.closed)

    def test_pagination_handles_a_server_page_cap_and_stops_at_seen_items(self) -> None:
        client = FakeClient(
            {
                1: {
                    "nbTotalCommunication": 4,
                    "myCommunicationList": [communication(4), communication(3)],
                },
                2: {
                    "nbTotalCommunication": 4,
                    "myCommunicationList": [communication(2), communication(1)],
                },
            }
        )

        result = watcher.collect_unseen_communications(client, seen={"1", "2"})  # type: ignore[arg-type]

        self.assertEqual(
            [communication_id for communication_id, _ in result], ["3", "4"]
        )
        self.assertEqual(client.requested_pages, [1, 2])

    def test_poll_account_refreshes_an_expired_session_once_then_retries(self) -> None:
        account = fake_account(Path("/tmp/account"))
        with (
            patch.object(watcher, "exclusive_lock", return_value=nullcontext()),
            patch("builtins.print"),
            patch.object(
                watcher,
                "run_poll",
                side_effect=[watcher.SessionExpired("expired"), 2],
            ) as run_poll,
            patch.object(watcher, "refresh_session") as refresh_session,
        ):
            self.assertEqual(watcher.poll_account(account), 2)

        self.assertEqual(run_poll.call_count, 2)
        refresh_session.assert_called_once_with(account)

    def test_corrupt_state_is_not_silently_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_file = Path(temporary_directory) / "state.json"
            state_file.write_text("not json", encoding="utf-8")
            account = fake_account(Path(temporary_directory))
            with self.assertRaises(watcher.StateError):
                watcher.load_state(account)

    def test_multi_account_config_uses_per_account_downloads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment = {
                "MYGUICHET_ACCOUNTS": "alice,bob",
                "MYGUICHET_ALICE_LUXTRUST_USERNAME": "alice-user",
                "MYGUICHET_ALICE_LUXTRUST_PASSWORD": "alice-password",
                "MYGUICHET_ALICE_SPACE_ID": "123",
                "MYGUICHET_ALICE_DOWNLOAD_DIR": str(root / "alice-docs"),
                "MYGUICHET_BOB_LUXTRUST_USERNAME": "bob-user",
                "MYGUICHET_BOB_LUXTRUST_PASSWORD": "bob-password",
                "MYGUICHET_BOB_SPACE_ID": "456",
                "MYGUICHET_BOB_DOWNLOAD_DIR": str(root / "bob-docs"),
            }
            with patch.dict("os.environ", environment, clear=True):
                with patch.object(config, "ROOT", root):
                    alice, bob = config.get_accounts()

            self.assertEqual(alice.name, "alice")
            self.assertEqual(alice.download_dir, root / "alice-docs")
            self.assertEqual(
                alice.cookie_file, root / "accounts" / "alice" / "cookie.txt"
            )
            self.assertEqual(bob.name, "bob")
            self.assertEqual(bob.download_dir, root / "bob-docs")

    def test_mqtt_trigger_payload_selects_accounts(self) -> None:
        self.assertIsNone(mqtt_trigger.parse_trigger_payload("").account_names)
        self.assertIsNone(mqtt_trigger.parse_trigger_payload("all").account_names)
        self.assertEqual(
            mqtt_trigger.parse_trigger_payload("alice").account_names, ["alice"]
        )
        self.assertEqual(
            mqtt_trigger.parse_trigger_payload(
                '{"accounts":["alice","bob"]}'
            ).account_names,
            ["alice", "bob"],
        )


if __name__ == "__main__":
    unittest.main()
