"""Offline checks for the watcher helpers; no real portal account is used."""

from __future__ import annotations

import queue
import sys
import tempfile
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch


WATCHER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WATCHER_ROOT))

import watcher_core  # noqa: E402
import mqtt_trigger  # noqa: E402
import config  # noqa: E402
from input_broker import (  # noqa: E402
    InputChallenge,
    InputTimeoutError,
    InputUnavailableError,
    PushInputBroker,
)
from outputs import register_output  # noqa: E402
from outputs.base import LocalDocument, OutputConfig, OutputError  # noqa: E402
from outputs.folder import FolderOutput  # noqa: E402
from sources import register_source  # noqa: E402
from sources.dkv import (  # noqa: E402
    DkvDocumentSource,
    collect_treated_refunds,
    documents_from_refund_detail,
)
from sources.dkv.client import DkvClient  # noqa: E402
from sources.myguichet import REQUESTS_PER_PAGE, collect_unseen_communications  # noqa: E402
from sources.myguichet.config import myguichet_account_from_source  # noqa: E402
from sources.base import (  # noqa: E402
    SourceAccountConfig,
    SourceContext,
    SourceDocument,
    SourceError,
    SourceMessage,
    SourceResponseError,
    SourceSessionExpired,
)
from storage import prepare_output_directory  # noqa: E402


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
        if per_page != REQUESTS_PER_PAGE:
            raise AssertionError("Unexpected page size")


class FakeDkvListClient:
    def __init__(self, pages: dict[int, dict[str, object]]) -> None:
        self.pages = pages
        self.requested_pages: list[tuple[int, int]] = []

    def list_refunds(self, page_index: int, limit: int) -> dict[str, object]:
        self.requested_pages.append((page_index, limit))
        return self.pages[page_index]


class FakeDkvAuthClient:
    def __init__(self) -> None:
        self.access_token = ""
        self.closed = False
        self.completed_otp = ""

    def start_sms_login(
        self, username: str, password: str, otp_type: str
    ) -> str | dict[str, object]:
        self.username = username
        self.password = password
        self.otp_type = otp_type
        return "session-token"

    def complete_sms_login(
        self, session_token: str, otp: str, otp_type: str
    ) -> dict[str, object]:
        self.session_token = session_token
        self.completed_otp = otp
        self.completed_otp_type = otp_type
        return {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_in": 300,
            "refresh_expires_in": 3600,
        }

    def refresh_access_token(self, refresh_token: str) -> dict[str, object]:
        raise AssertionError(f"Unexpected refresh token use: {refresh_token}")

    def set_access_token(self, access_token: str) -> None:
        self.access_token = access_token

    def list_refunds(self, page_index: int, limit: int) -> dict[str, object]:
        del page_index, limit
        return {"groups": [{"items": []}], "pagingInfo": {"limit": 20, "offset": 0, "total": 0}}

    def close(self) -> None:
        self.closed = True


class FakeHttpResponse:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeHttpSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object) -> FakeHttpResponse:
        self.calls.append((url, kwargs))
        return FakeHttpResponse()


class PartlyFailingSource:
    name = "partly_failing"

    def collect_unseen(
        self, account: SourceAccountConfig, context: SourceContext, seen: set[str]
    ) -> list[SourceMessage]:
        del account, context, seen
        return [SourceMessage("1"), SourceMessage("2")]

    def list_documents(
        self, account: SourceAccountConfig, context: SourceContext, message: SourceMessage
    ) -> list[SourceDocument]:
        del account, context
        if message.id == "1":
            raise SourceResponseError("temporary source failure")
        return [SourceDocument("doc-2", "document.txt", "text/plain")]

    def open_document(
        self,
        account: SourceAccountConfig,
        context: SourceContext,
        message: SourceMessage,
        document: SourceDocument,
    ) -> FakeResponse:
        del account, context, message, document
        return FakeResponse([b"downloaded"], "text/plain")

    def refresh_authentication(
        self, account: SourceAccountConfig, context: SourceContext
    ) -> None:
        del account, context

    def close(self) -> None:
        pass


class AlwaysFailingOutput:
    name = "always_failing"

    def deliver(
        self,
        account: SourceAccountConfig,
        config: OutputConfig,
        message: SourceMessage,
        document: SourceDocument,
        local_document: LocalDocument,
    ) -> None:
        del account, config, message, document, local_document
        raise OutputError("output refused document")

    def close(self) -> None:
        pass


class ExpiringSource:
    name = "expiring"
    refreshed = False

    def collect_unseen(
        self, account: SourceAccountConfig, context: SourceContext, seen: set[str]
    ) -> list[SourceMessage]:
        del account, context, seen
        if not ExpiringSource.refreshed:
            raise SourceSessionExpired("expired")
        return []

    def list_documents(
        self, account: SourceAccountConfig, context: SourceContext, message: SourceMessage
    ) -> list[SourceDocument]:
        del account, context, message
        return []

    def open_document(
        self,
        account: SourceAccountConfig,
        context: SourceContext,
        message: SourceMessage,
        document: SourceDocument,
    ) -> FakeResponse:
        del account, context, message, document
        return FakeResponse([b"content"])

    def refresh_authentication(
        self, account: SourceAccountConfig, context: SourceContext
    ) -> None:
        del account, context
        ExpiringSource.refreshed = True

    def close(self) -> None:
        pass


class StaticInputBroker:
    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.challenges: list[InputChallenge] = []

    def request_input(self, challenge: InputChallenge) -> dict[str, str]:
        self.challenges.append(challenge)
        return self.answers


class TimeoutInputBroker:
    def request_input(self, challenge: InputChallenge) -> dict[str, str]:
        raise InputTimeoutError(
            f"Input challenge {challenge.id} for {challenge.account_name} timed out."
        )


class OtpSource:
    name = "otp_source"
    received_code = ""

    def collect_unseen(
        self, account: SourceAccountConfig, context: SourceContext, seen: set[str]
    ) -> list[SourceMessage]:
        del seen
        answer = context.request_input(
            InputChallenge(
                account_name=account.name,
                source=account.source,
                kind="otp",
                prompt="Enter the one-time code",
                timeout_seconds=300,
            )
        )
        OtpSource.received_code = answer["code"]
        return []

    def list_documents(
        self, account: SourceAccountConfig, context: SourceContext, message: SourceMessage
    ) -> list[SourceDocument]:
        del account, context, message
        return []

    def open_document(
        self,
        account: SourceAccountConfig,
        context: SourceContext,
        message: SourceMessage,
        document: SourceDocument,
    ) -> FakeResponse:
        del account, context, message, document
        return FakeResponse([b"content"])

    def refresh_authentication(
        self, account: SourceAccountConfig, context: SourceContext
    ) -> None:
        del account, context

    def close(self) -> None:
        pass


class BinaryHeaderPdfSource:
    name = "binary_header_pdf"

    def collect_unseen(
        self, account: SourceAccountConfig, context: SourceContext, seen: set[str]
    ) -> list[SourceMessage]:
        del account, context, seen
        return [SourceMessage("refund-1", {"subject": "Refund"})]

    def list_documents(
        self, account: SourceAccountConfig, context: SourceContext, message: SourceMessage
    ) -> list[SourceDocument]:
        del account, context, message
        return [SourceDocument("IIS#18660489", "Statement", "application/pdf")]

    def open_document(
        self,
        account: SourceAccountConfig,
        context: SourceContext,
        message: SourceMessage,
        document: SourceDocument,
    ) -> FakeResponse:
        del account, context, message, document
        return FakeResponse([b"%PDF-1.4\n"], "application/octet-stream")

    def refresh_authentication(
        self, account: SourceAccountConfig, context: SourceContext
    ) -> None:
        del account, context

    def close(self) -> None:
        pass


def communication(communication_id: int) -> dict[str, object]:
    return {"eDeliveryCommunicationHitDto": {"id": communication_id}}


def refund(refund_id: str, status_code: str) -> dict[str, object]:
    return {
        "id": refund_id,
        "title": f"Refund {refund_id}",
        "description": "Reimbursement",
        "status": status_code.title(),
        "statusCode": status_code,
    }


def fake_source_account(
    root: Path, output_configs: tuple[OutputConfig, ...] | None = None
) -> SourceAccountConfig:
    if output_configs is None:
        output_configs = (
            OutputConfig(
                "folder",
                "folder",
                {
                    "directory": root / "downloads",
                    "file_mode": 0o600,
                    "dir_mode": 0o700,
                },
            ),
        )
    return SourceAccountConfig(
        name="alice",
        source="partly_failing",
        maximum_document_mb=100,
        runtime_dir=root,
        state_file=root / "state.json",
        lock_file=root / ".run.lock",
        output_configs=output_configs,
    )


class WatcherHelpersTest(unittest.TestCase):
    def test_safe_filename_removes_path_characters_and_controls(self) -> None:
        self.assertEqual(watcher_core.safe_filename(" ../a/b\\c\x00.pdf "), "_a_b_c_.pdf")

    def test_document_filename_uses_message_metadata_when_available(self) -> None:
        metadata = {
            "sentDate": "29/07/2026 12:15:59",
            "sender": {"name": "Caisse nationale de santé"},
            "subject": "Détail de remboursement",
        }
        filename = watcher_core.document_filename(
            SourceMessage("987654", metadata),
            SourceDocument("123456", "document.pdf", "application/pdf", {}),
        )

        self.assertEqual(
            filename,
            "2026-07-29_121559_Caisse nationale de santé_"
            "Détail de remboursement_987654_123456_document.pdf",
        )

    def test_document_filename_falls_back_to_ids(self) -> None:
        filename = watcher_core.document_filename(
            SourceMessage("987654"),
            SourceDocument("123456", "document", "application/pdf", {}),
        )

        self.assertEqual(filename, "987654_123456_document.pdf")

    def test_generic_binary_response_keeps_plugin_pdf_extension(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            account = fake_source_account(root)

            with patch("builtins.print"):
                watcher_core.run_poll(account, BinaryHeaderPdfSource())

            downloaded = list((root / "downloads").iterdir())
            self.assertEqual(len(downloaded), 1)
            self.assertTrue(downloaded[0].name.endswith(".pdf"))
            self.assertFalse(downloaded[0].name.endswith(".bin"))

    def test_write_document_is_private_and_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "private" / "document.pdf"
            response = FakeResponse([b"part one", b"part two"])

            watcher_core.write_document(response, destination, maximum_bytes=1024)

            self.assertEqual(destination.read_bytes(), b"part onepart two")
            self.assertTrue(response.closed)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

    def test_write_document_can_use_shared_file_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "document.pdf"
            response = FakeResponse([b"content"])

            watcher_core.write_document(
                response, destination, maximum_bytes=1024, file_mode=0o644
            )

            self.assertEqual(destination.read_bytes(), b"content")
            self.assertEqual(destination.stat().st_mode & 0o777, 0o644)

    def test_empty_document_is_not_published(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "document.pdf"
            response = FakeResponse([])

            with self.assertRaises(SourceResponseError):
                watcher_core.write_document(response, destination, maximum_bytes=1024)

            self.assertFalse(destination.exists())
            self.assertTrue(response.closed)

    def test_oversized_document_is_closed_without_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "document.pdf"
            response = FakeResponse([b"small"])
            response.headers["Content-Length"] = "1000"

            with self.assertRaises(SourceResponseError):
                watcher_core.write_document(response, destination, maximum_bytes=100)

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

        result = collect_unseen_communications(client, seen={"1", "2"})  # type: ignore[arg-type]

        self.assertEqual(
            [communication_id for communication_id, _ in result], ["3", "4"]
        )
        self.assertEqual(client.requested_pages, [1, 2])

    def test_dkv_pagination_collects_only_unseen_treated_refunds_to_the_end(self) -> None:
        client = FakeDkvListClient(
            {
                0: {
                    "groups": [
                        {
                            "items": [
                                refund("REFUND#newest-treated", "TREATED"),
                                refund("REFUND#sent", "SENT"),
                            ]
                        }
                    ],
                    "pagingInfo": {"limit": 2, "offset": 0, "total": 4},
                },
                1: {
                    "groups": [
                        {
                            "items": [
                                refund("REFUND#old-treated", "TREATED"),
                                refund("REFUND#seen-treated", "TREATED"),
                            ]
                        }
                    ],
                    "pagingInfo": {"limit": 2, "offset": 2, "total": 4},
                },
            }
        )

        result = collect_treated_refunds(
            client, seen={"REFUND#seen-treated"}, page_limit=2  # type: ignore[arg-type]
        )

        self.assertEqual(
            [refund_id for refund_id, _ in result],
            ["REFUND#old-treated", "REFUND#newest-treated"],
        )
        self.assertEqual(client.requested_pages, [(0, 2), (1, 2)])

    def test_dkv_refund_detail_maps_downloadable_documents(self) -> None:
        documents = documents_from_refund_detail(
            "REFUND#1",
            {
                "id": "REFUND#1",
                "statusCode": "TREATED",
                "listDocument": [
                    {
                        "idDocument": "IIS#18660489",
                        "label": "Statement",
                        "logo": "pdf",
                        "order": 1,
                    }
                ],
            },
        )

        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0].id, "IIS#18660489")
        self.assertEqual(documents[0].name, "Statement")
        self.assertEqual(documents[0].content_type, "application/pdf")

    def test_dkv_refund_detail_rejects_sent_reimbursements(self) -> None:
        with self.assertRaises(SourceResponseError):
            documents_from_refund_detail(
                "REFUND_SUBMIT#1",
                {"id": "REFUND_SUBMIT#1", "statusCode": "SENT", "listDocument": []},
            )

    def test_dkv_plugin_requests_sms_otp_and_persists_token(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary_directory,
            patch("builtins.print"),
        ):
            root = Path(temporary_directory)
            fake_client = FakeDkvAuthClient()
            account = SourceAccountConfig(
                name="alice_dkv",
                source="dkv",
                maximum_document_mb=100,
                runtime_dir=root,
                state_file=root / "state.json",
                lock_file=root / ".run.lock",
                source_settings={
                    "username": "alice-user",
                    "password": "secret",
                    "otp_timeout_seconds": "300",
                },
            )
            broker = StaticInputBroker({"code": "654321"})

            count = watcher_core.run_poll(
                account,
                DkvDocumentSource(lambda: fake_client),  # type: ignore[arg-type]
                SourceContext(input_broker=broker),
            )

            token_file = root / "dkv_token.json"
            self.assertEqual(count, 0)
            self.assertEqual(fake_client.completed_otp, "654321")
            self.assertEqual(fake_client.access_token, "access-token")
            self.assertTrue(token_file.exists())
            self.assertIn("access-token", token_file.read_text(encoding="utf-8"))
            self.assertEqual(broker.challenges[0].account_name, "alice_dkv")
            self.assertEqual(broker.challenges[0].source, "dkv")
            self.assertEqual(broker.challenges[0].timeout_seconds, 300)

    def test_dkv_client_downloads_document_by_encoded_document_id(self) -> None:
        client = DkvClient()
        fake_session = FakeHttpSession()
        client.session = fake_session  # type: ignore[assignment]

        response = client.download_document("IIS#18660489")

        self.assertFalse(response.closed)
        self.assertEqual(
            fake_session.calls[0][0],
            "https://api-client-external-secure.lalux-partners.lu/documents/IIS%2318660489",
        )
        self.assertEqual(fake_session.calls[0][1]["params"], {"idFile": "IIS#18660489"})
        self.assertTrue(fake_session.calls[0][1]["stream"])

    def test_poll_account_refreshes_an_expired_session_once_then_retries(self) -> None:
        ExpiringSource.refreshed = False
        register_source("expiring", ExpiringSource)
        with (
            tempfile.TemporaryDirectory() as temporary_directory,
            patch.object(watcher_core, "exclusive_lock", return_value=nullcontext()),
            patch("builtins.print"),
        ):
            root = Path(temporary_directory)
            account = SourceAccountConfig(
                name="alice",
                source="expiring",
                maximum_document_mb=100,
                runtime_dir=root,
                state_file=root / "state.json",
                lock_file=root / ".run.lock",
            )
            self.assertEqual(watcher_core.poll_account(account), 0)

        self.assertTrue(ExpiringSource.refreshed)

    def test_source_can_request_external_input_through_context(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary_directory,
            patch("builtins.print"),
        ):
            root = Path(temporary_directory)
            account = SourceAccountConfig(
                name="alice_otp_source",
                source="otp_source",
                maximum_document_mb=100,
                runtime_dir=root,
                state_file=root / "state.json",
                lock_file=root / ".run.lock",
            )
            broker = StaticInputBroker({"code": "123456"})

            count = watcher_core.run_poll(
                account, OtpSource(), SourceContext(input_broker=broker)
            )

        self.assertEqual(count, 0)
        self.assertEqual(OtpSource.received_code, "123456")
        self.assertEqual(broker.challenges[0].account_name, "alice_otp_source")
        self.assertEqual(broker.challenges[0].source, "otp_source")
        self.assertEqual(broker.challenges[0].timeout_seconds, 300)

    def test_input_timeout_aborts_account_without_hanging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            account = SourceAccountConfig(
                name="alice_otp_source",
                source="otp_source",
                maximum_document_mb=100,
                runtime_dir=root,
                state_file=root / "state.json",
                lock_file=root / ".run.lock",
            )

            with self.assertRaises(SourceError):
                watcher_core.run_poll(
                    account,
                    OtpSource(),
                    SourceContext(input_broker=TimeoutInputBroker()),
                )

            self.assertFalse(account.state_file.exists())

    def test_push_input_broker_accepts_early_generic_fields_by_account(self) -> None:
        broker = PushInputBroker(early_answer_ttl_seconds=10)
        with patch("builtins.print"):
            broker.provide("ALICE_OTHER", {"answer": "blue", "device": "phone"})

            answer = broker.request_input(
                InputChallenge(
                    account_name="alice_other",
                    source="other",
                    kind="security_question",
                    prompt="Answer the security question",
                    fields=("answer", "device"),
                    timeout_seconds=1,
                )
            )

        self.assertEqual(answer, {"answer": "blue", "device": "phone"})

    def test_push_input_broker_rejects_answers_missing_requested_fields(self) -> None:
        broker = PushInputBroker(early_answer_ttl_seconds=10)
        with patch("builtins.print"):
            broker.provide("alice_other", {"code": "123456"})

            with self.assertRaises(InputUnavailableError):
                broker.request_input(
                    InputChallenge(
                        account_name="alice_other",
                        source="other",
                        kind="approval",
                        prompt="Enter all approval fields",
                        fields=("code", "pin"),
                        timeout_seconds=1,
                    )
                )

    def test_push_input_broker_handles_multiple_pending_accounts(self) -> None:
        broker = PushInputBroker(early_answer_ttl_seconds=10)
        results: "queue.Queue[tuple[str, dict[str, str]]]" = queue.Queue()

        def wait_for_code(account_name: str) -> None:
            answer = broker.request_input(
                InputChallenge(
                    account_name=account_name,
                    source=account_name.rsplit("_", 1)[-1],
                    kind="otp",
                    prompt="Enter the one-time code",
                    timeout_seconds=2,
                )
            )
            results.put((account_name, answer))

        threads = [
            threading.Thread(target=wait_for_code, args=("alice_myguichet",)),
            threading.Thread(target=wait_for_code, args=("bob_dkv",)),
        ]
        with patch("builtins.print"):
            for thread in threads:
                thread.start()

            broker.provide("bob_dkv", {"code": "222222"})
            broker.provide("alice_myguichet", {"code": "111111"})

            for thread in threads:
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())

        received = dict(results.get_nowait() for _ in threads)
        self.assertEqual(received["alice_myguichet"], {"code": "111111"})
        self.assertEqual(received["bob_dkv"], {"code": "222222"})

    def test_corrupt_state_is_not_silently_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_file = Path(temporary_directory) / "state.json"
            state_file.write_text("not json", encoding="utf-8")
            account = fake_source_account(Path(temporary_directory))
            with self.assertRaises(watcher_core.StateError):
                watcher_core.load_state(account)

    def test_document_accounts_are_required(self) -> None:
        with patch.dict("os.environ", {"MYGUICHET_ACCOUNTS": "alice"}, clear=True):
            with self.assertRaises(config.ConfigurationError):
                config.get_source_accounts()

    def test_multi_user_config_infers_source_accounts_from_plugin_prefixes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment = {
                "DOCUMENT_USERS": "alice,bob",
                "DOCUMENT_ALICE_MYGUICHET_LUXTRUST_USERNAME": "alice-user",
                "DOCUMENT_ALICE_MYGUICHET_LUXTRUST_PASSWORD": "alice-password",
                "DOCUMENT_ALICE_MYGUICHET_SPACE_ID": "123",
                "DOCUMENT_ALICE_MYGUICHET_OUTPUT_FOLDER_DIRECTORY": str(root / "alice-docs"),
                "DOCUMENT_BOB_MYGUICHET_LUXTRUST_USERNAME": "bob-user",
                "DOCUMENT_BOB_MYGUICHET_LUXTRUST_PASSWORD": "bob-password",
                "DOCUMENT_BOB_MYGUICHET_SPACE_ID": "456",
                "DOCUMENT_BOB_MYGUICHET_OUTPUT_FOLDER_DIRECTORY": str(root / "bob-docs"),
            }
            with patch.dict("os.environ", environment, clear=True):
                with patch.object(config, "ROOT", root):
                    alice, bob = config.get_source_accounts()

            self.assertEqual(alice.name, "alice_myguichet")
            self.assertEqual(alice.source, "myguichet")
            self.assertEqual(
                alice.output_configs[0].settings["directory"], str(root / "alice-docs")
            )
            self.assertEqual(bob.name, "bob_myguichet")
            self.assertEqual(bob.source_settings["space_id"], "456")
            self.assertEqual(
                bob.output_configs[0].settings["directory"], str(root / "bob-docs")
            )

    def test_users_can_have_different_source_sets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment = {
                "DOCUMENT_USERS": "alice,bob",
                "DOCUMENT_ALICE_SOURCES": "myguichet",
                "DOCUMENT_ALICE_MYGUICHET_SPACE_ID": "123",
                "DOCUMENT_ALICE_MYGUICHET_OUTPUT_FOLDER_DIRECTORY": str(
                    root / "alice-myguichet"
                ),
                "DOCUMENT_BOB_SOURCES": "myguichet,otherservice",
                "DOCUMENT_BOB_MYGUICHET_SPACE_ID": "456",
                "DOCUMENT_BOB_MYGUICHET_OUTPUT_FOLDER_DIRECTORY": str(
                    root / "bob-myguichet"
                ),
                "DOCUMENT_BOB_OTHERSERVICE_USERNAME": "bob-other-user",
                "DOCUMENT_BOB_OTHERSERVICE_OUTPUT_FOLDER_DIRECTORY": str(
                    root / "bob-other"
                ),
            }
            with patch.dict("os.environ", environment, clear=True):
                with patch.object(config, "ROOT", root):
                    accounts = config.get_source_accounts()

            self.assertEqual(
                [(account.name, account.source) for account in accounts],
                [
                    ("alice_myguichet", "myguichet"),
                    ("bob_myguichet", "myguichet"),
                    ("bob_otherservice", "otherservice"),
                ],
            )
            self.assertEqual(
                accounts[2].output_configs[0].settings["directory"],
                str(root / "bob-other"),
            )

    def test_document_account_config_supports_named_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment = {
                "DOCUMENT_USERS": "alice",
                "DOCUMENT_ALICE_SOURCES": "taxbox",
                "DOCUMENT_ALICE_TAXBOX_OUTPUT_FOLDER_DIRECTORY": str(root / "taxbox-docs"),
            }
            with patch.dict("os.environ", environment, clear=True):
                with patch.object(config, "ROOT", root):
                    account = config.get_source_account("alice_taxbox")

            self.assertEqual(account.name, "alice_taxbox")
            self.assertEqual(account.source, "taxbox")
            self.assertEqual(account.output_configs[0].type, "folder")
            self.assertEqual(
                account.output_configs[0].settings["directory"],
                str(root / "taxbox-docs"),
            )
            self.assertEqual(
                account.state_file, root / "accounts" / "alice_taxbox" / "state.json"
            )

    def test_document_account_config_supports_named_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment = {
                "DOCUMENT_USERS": "alice",
                "DOCUMENT_ALICE_SOURCES": "taxbox",
                "DOCUMENT_ALICE_TAXBOX_OUTPUTS": "paperless:folder,mailer:email",
                "DOCUMENT_ALICE_TAXBOX_OUTPUT_PAPERLESS_DIRECTORY": str(root / "paperless"),
                "DOCUMENT_ALICE_TAXBOX_OUTPUT_PAPERLESS_FILE_MODE": "0644",
                "DOCUMENT_ALICE_TAXBOX_OUTPUT_MAILER_URL": "https://example.invalid/send",
            }
            with patch.dict("os.environ", environment, clear=True):
                with patch.object(config, "ROOT", root):
                    account = config.get_source_account("alice:taxbox")

            paperless, mailer = account.output_configs
            self.assertEqual((paperless.name, paperless.type), ("paperless", "folder"))
            self.assertEqual(paperless.settings["directory"], str(root / "paperless"))
            self.assertEqual(paperless.settings["file_mode"], "0644")
            self.assertEqual((mailer.name, mailer.type), ("mailer", "email"))
            self.assertEqual(mailer.settings["url"], "https://example.invalid/send")

    def test_myguichet_plugin_owns_source_setting_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            account = SourceAccountConfig(
                name="alice_myguichet",
                source="myguichet",
                maximum_document_mb=100,
                runtime_dir=root,
                state_file=root / "state.json",
                lock_file=root / ".run.lock",
                source_settings={
                    "luxtrust_username": "alice-user",
                    "luxtrust_password": "secret",
                    "space_id": "123456",
                    "language": "de",
                    "headless": "true",
                    "login_timeout_seconds": "30",
                },
            )

            myguichet = myguichet_account_from_source(account)

            self.assertEqual(myguichet.space_id, "123456")
            self.assertEqual(myguichet.language, "de")
            self.assertTrue(myguichet.headless)
            self.assertEqual(myguichet.login_timeout_seconds, 30)
            self.assertEqual(myguichet.cookie_file, root / "cookie.txt")

    def test_folder_plugin_owns_output_setting_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            staged = root / "staged.txt"
            staged.write_text("content", encoding="utf-8")
            account = fake_source_account(root)

            FolderOutput().deliver(
                account,
                OutputConfig(
                    "local",
                    "folder",
                    {
                        "directory": str(root / "out"),
                        "file_mode": "0644",
                        "dir_mode": "0755",
                    },
                ),
                SourceMessage("1"),
                SourceDocument("doc-1", "document.txt", "text/plain"),
                LocalDocument(staged, "document.txt", "text/plain", staged.stat().st_size),
            )

            destination = root / "out" / "document.txt"
            self.assertEqual(destination.read_text(encoding="utf-8"), "content")
            self.assertEqual(destination.stat().st_mode & 0o777, 0o644)
            self.assertEqual(destination.parent.stat().st_mode & 0o777, 0o755)

    def test_one_failed_message_does_not_block_later_messages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            account = fake_source_account(root)

            with patch("builtins.print"):
                with self.assertRaises(SourceError):
                    watcher_core.run_poll(account, PartlyFailingSource())

            state = watcher_core.load_state(account)
            self.assertEqual(state["seen_ids"], ["2"])
            downloaded = list((root / "downloads").iterdir())
            self.assertEqual(len(downloaded), 1)
            self.assertEqual(downloaded[0].read_bytes(), b"downloaded")

    def test_output_failure_prevents_message_checkpoint(self) -> None:
        register_output("always_failing", AlwaysFailingOutput)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            account = fake_source_account(
                root, (OutputConfig("bad", "always_failing"),)
            )

            with patch("builtins.print"):
                with self.assertRaises(SourceError):
                    watcher_core.run_poll(account, PartlyFailingSource())

            self.assertFalse(account.state_file.exists())

    def test_download_directory_mode_is_not_changed_when_it_already_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            existing_output = root / "paperless-consume"
            existing_output.mkdir()
            existing_output.chmod(0o775)

            prepare_output_directory(existing_output, 0o700)

            self.assertEqual(existing_output.stat().st_mode & 0o777, 0o775)

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

    def test_mqtt_input_payload_accepts_code_shorthand(self) -> None:
        request = mqtt_trigger.parse_input_payload(
            '{"for":"fede_myguichet","code":"7ahdr"}'
        )

        self.assertEqual(request.account_name, "fede_myguichet")
        self.assertEqual(request.fields, {"code": "7ahdr"})

    def test_mqtt_input_payload_accepts_generic_fields(self) -> None:
        request = mqtt_trigger.parse_input_payload(
            '{"account":"fede_other","fields":{"answer":"blue","device":"phone"}}'
        )

        self.assertEqual(request.account_name, "fede_other")
        self.assertEqual(request.fields, {"answer": "blue", "device": "phone"})

    def test_mqtt_input_payload_requires_fields(self) -> None:
        with self.assertRaises(config.ConfigurationError):
            mqtt_trigger.parse_input_payload('{"for":"fede_other"}')

    def test_mqtt_input_topic_must_differ_from_trigger_topic(self) -> None:
        environment = {
            "DOCUMENT_MQTT_HOST": "localhost",
            "DOCUMENT_MQTT_TOPIC": "documents/input",
            "DOCUMENT_MQTT_INPUT_TOPIC": "documents/input",
        }
        with (
            patch.dict("os.environ", environment, clear=True),
            patch.object(mqtt_trigger, "load_environment"),
            patch("builtins.print"),
        ):
            self.assertEqual(mqtt_trigger.main(), 1)

    def test_mqtt_worker_passes_push_input_broker_to_poll_account(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            account = SourceAccountConfig(
                name="alice_myguichet",
                source="myguichet",
                maximum_document_mb=100,
                runtime_dir=root,
                state_file=root / "state.json",
                lock_file=root / ".run.lock",
            )
            jobs: "queue.Queue[SourceAccountConfig]" = queue.Queue()
            broker = PushInputBroker()

            worker_thread = threading.Thread(
                target=mqtt_trigger.worker,
                args=(object(), jobs, broker),
                daemon=True,
            )
            with patch.object(mqtt_trigger, "poll_account", return_value=7) as mocked:
                with patch("builtins.print"):
                    worker_thread.start()
                    jobs.put(account)
                    jobs.join()

            mocked.assert_called_once_with(account, input_broker=broker)


if __name__ == "__main__":
    unittest.main()
