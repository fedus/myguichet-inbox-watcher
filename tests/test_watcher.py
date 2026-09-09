"""Offline checks for the watcher helpers; no real portal account is used."""

from __future__ import annotations

import json
import queue
import sys
import tempfile
import threading
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch


WATCHER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WATCHER_ROOT))

import watcher_core  # noqa: E402
import mqtt_trigger  # noqa: E402
import config  # noqa: E402
import api_server  # noqa: E402
from input_broker import (  # noqa: E402
    InputChallenge,
    InputTimeoutError,
    InputUnavailableError,
    PushInputBroker,
)
from runtime_state import RuntimeState  # noqa: E402
from outputs import register_output  # noqa: E402
from outputs.base import (  # noqa: E402
    LocalDocument,
    OutputConfig,
    OutputError,
    OutputPollResult,
)
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


class FakeMqttStatusClient:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []
        self.disconnected = False

    def publish(self, topic: str, payload: str) -> None:
        self.published.append((topic, payload))

    def disconnect(self) -> None:
        self.disconnected = True


class FakeConfigProvider:
    def __init__(self, accounts: list[SourceAccountConfig] | None = None) -> None:
        self.accounts = accounts or []
        self.loaded = False

    def load(self) -> None:
        self.loaded = True

    def get_source_account(self, name: str) -> SourceAccountConfig:
        for account in self.accounts:
            if account.name == name:
                return account
        raise config.ConfigurationError(f"Unknown configured account {name!r}.")

    def get_source_accounts(self) -> list[SourceAccountConfig]:
        return list(self.accounts)


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


class TwoMessageSource:
    name = "two_message"

    def collect_unseen(
        self, account: SourceAccountConfig, context: SourceContext, seen: set[str]
    ) -> list[SourceMessage]:
        del account, context, seen
        return [SourceMessage("1"), SourceMessage("2")]

    def list_documents(
        self, account: SourceAccountConfig, context: SourceContext, message: SourceMessage
    ) -> list[SourceDocument]:
        del account, context
        return [SourceDocument(f"doc-{message.id}", f"document-{message.id}.txt", "text/plain")]

    def open_document(
        self,
        account: SourceAccountConfig,
        context: SourceContext,
        message: SourceMessage,
        document: SourceDocument,
    ) -> FakeResponse:
        del account, context, document
        return FakeResponse([f"content-{message.id}".encode("utf-8")], "text/plain")

    def refresh_authentication(
        self, account: SourceAccountConfig, context: SourceContext
    ) -> None:
        del account, context

    def close(self) -> None:
        pass


class BatchRecordingOutput:
    name = "batch_recording"
    events: list[str] = []
    delivered: list[str] = []
    result: OutputPollResult | None = None

    def begin_poll(self, account: SourceAccountConfig, config: OutputConfig) -> None:
        del account, config
        type(self).events.append("begin")

    def deliver(
        self,
        account: SourceAccountConfig,
        config: OutputConfig,
        message: SourceMessage,
        document: SourceDocument,
        local_document: LocalDocument,
    ) -> None:
        del account, config, document
        type(self).events.append(f"deliver:{message.id}")
        type(self).delivered.append(local_document.path.read_text(encoding="utf-8"))

    def end_poll(
        self,
        account: SourceAccountConfig,
        config: OutputConfig,
        result: OutputPollResult,
    ) -> None:
        del account, config
        type(self).events.append("end")
        type(self).result = result

    def close(self) -> None:
        type(self).events.append("close")


class BatchFailingOutput(BatchRecordingOutput):
    name = "batch_failing"
    events: list[str] = []
    delivered: list[str] = []
    result: OutputPollResult | None = None

    def end_poll(
        self,
        account: SourceAccountConfig,
        config: OutputConfig,
        result: OutputPollResult,
    ) -> None:
        del account, config, result
        type(self).events.append("end")
        raise OutputError("could not finalize batch")


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

    def test_push_input_broker_close_aborts_pending_request(self) -> None:
        broker = PushInputBroker(early_answer_ttl_seconds=10)
        errors: "queue.Queue[str]" = queue.Queue()

        def wait_for_input() -> None:
            try:
                broker.request_input(
                    InputChallenge(
                        account_name="alice_dkv",
                        source="dkv",
                        kind="otp",
                        prompt="Enter the one-time code",
                        timeout_seconds=10,
                    )
                )
            except InputUnavailableError as error:
                errors.put(str(error))

        with patch("builtins.print"):
            thread = threading.Thread(target=wait_for_input)
            thread.start()
            time.sleep(0.05)
            broker.close("Runner is shutting down.")
            thread.join(timeout=1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors.get_nowait(), "Runner is shutting down.")
        with self.assertRaises(InputUnavailableError):
            broker.provide("alice_dkv", {"code": "123456"})

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

    def test_env_config_provider_reads_accounts_from_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            provider = config.EnvConfigProvider(
                environ={
                    "DOCUMENT_USERS": "alice",
                    "DOCUMENT_ALICE_SOURCES": "myguichet",
                    "DOCUMENT_ALICE_MYGUICHET_SPACE_ID": "123",
                    "DOCUMENT_ALICE_MYGUICHET_OUTPUTS": "local:folder",
                    "DOCUMENT_ALICE_MYGUICHET_OUTPUT_LOCAL_DIRECTORY": "downloads/alice",
                },
                root=root,
            )

            account = provider.get_source_account("alice_myguichet")

        self.assertEqual(account.name, "alice_myguichet")
        self.assertEqual(account.source, "myguichet")
        self.assertEqual(account.runtime_dir, root / "accounts" / "alice_myguichet")
        self.assertEqual(account.source_settings["space_id"], "123")
        self.assertEqual(account.output_configs[0].name, "local")
        self.assertEqual(account.output_configs[0].type, "folder")
        self.assertEqual(
            account.output_configs[0].settings["directory"], "downloads/alice"
        )

    def test_env_config_provider_loads_dotenv_without_overriding_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            env_file = root / ".env"
            env_file.write_text(
                "DOCUMENT_USERS=from_file\nDOCUMENT_VALUE=from_file\n",
                encoding="utf-8",
            )
            environ = {"DOCUMENT_VALUE": "from_env"}
            provider = config.EnvConfigProvider(environ=environ, root=root)

            provider.load()

        self.assertEqual(environ["DOCUMENT_USERS"], "from_file")
        self.assertEqual(environ["DOCUMENT_VALUE"], "from_env")

    def test_api_redacts_credential_like_settings(self) -> None:
        result = api_server.sanitize_settings(
            {
                "username": "alice",
                "password": "secret",
                "refresh_token": "token",
                "space_id": "123",
            }
        )

        self.assertEqual(result["username"], "***")
        self.assertEqual(result["password"], "***")
        self.assertEqual(result["refresh_token"], "***")
        self.assertEqual(result["space_id"], "123")

    def test_dashboard_assets_are_served_separately(self) -> None:
        index = (api_server.DASHBOARD_DIR / "index.html").read_text(
            encoding="utf-8"
        )
        javascript = (api_server.DASHBOARD_DIR / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn('href="/assets/styles.css"', index)
        self.assertIn('src="/assets/app.js"', index)
        self.assertNotIn("<style>", index)
        self.assertNotIn("<script>", index)
        self.assertIn('id="serviceBadges"', index)
        self.assertIn('id="outputConfig"', index)
        self.assertIn("const $ = (id) => document.getElementById(id);", javascript)
        self.assertIn("API error:", javascript)
        self.assertIn("function renderOutputChip(account, output, index)", javascript)
        self.assertIn("function showOutputConfig(accountName, outputIndex)", javascript)
        self.assertIn("fetchJson(\"/api/service\")", javascript)
        self.assertIn('class="config-list"', javascript)
        self.assertIn("'\"': \"&quot;\"", javascript)
        self.assertNotIn('""":', javascript)
        self.assertNotIn("accountCount.textContent", javascript)

    def test_api_service_payload_exposes_no_credentials(self) -> None:
        payload = api_server.service_payload(
            {
                "DOCUMENT_RUN_MODE": "watcher",
                "DOCUMENT_API_HOST": "0.0.0.0",
                "DOCUMENT_API_PORT": "8000",
                "DOCUMENT_MQTT_HOST": "mqtt.local",
                "DOCUMENT_MQTT_PORT": "1883",
                "DOCUMENT_MQTT_USERNAME": "alice",
                "DOCUMENT_MQTT_PASSWORD": "secret",
                "DOCUMENT_MQTT_TOPIC": "documents/poll",
                "DOCUMENT_MQTT_INPUT_TOPIC": "documents/input/provide",
                "DOCUMENT_MQTT_STATUS_TOPIC": "documents/poll/status",
                "DOCUMENT_MQTT_WORKERS": "2",
            }
        )

        self.assertEqual(payload["run_mode"], "watcher")
        self.assertEqual(payload["http"]["port"], "8000")
        self.assertTrue(payload["mqtt"]["configured"])
        self.assertTrue(payload["mqtt"]["running"])
        self.assertEqual(payload["mqtt"]["host"], "mqtt.local")
        self.assertEqual(payload["mqtt"]["workers"], "2")
        self.assertNotIn("username", payload["mqtt"])
        self.assertNotIn("password", payload["mqtt"])

    def test_api_exposes_configured_accounts_and_runtime_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            account = SourceAccountConfig(
                name="alice_dkv",
                source="dkv",
                maximum_document_mb=100,
                runtime_dir=root / "accounts" / "alice_dkv",
                state_file=root / "accounts" / "alice_dkv" / "state.json",
                lock_file=root / "accounts" / "alice_dkv" / ".run.lock",
                source_settings={"username": "alice", "otp_type": "SMS"},
                output_configs=(
                    OutputConfig(
                        "local",
                        "folder",
                        {"directory": str(root / "downloads" / "alice_dkv")},
                    ),
                ),
            )
            state = RuntimeState()
            state.poll_started("alice_dkv", "dkv")
            app = api_server.create_app(FakeConfigProvider([account]), state)

            account_payload = api_server.account_payload(account)
            status = state.snapshot()
            openapi_paths = app.openapi()["paths"]
            route_paths = {getattr(route, "path", "") for route in app.routes}

        self.assertIn("/api/health", openapi_paths)
        self.assertIn("/api/plugins", openapi_paths)
        self.assertIn("/api/accounts", openapi_paths)
        self.assertIn("/api/status", openapi_paths)
        self.assertIn("/api/service", openapi_paths)
        self.assertIn("/", route_paths)
        self.assertIn("/assets", route_paths)
        self.assertEqual(account_payload["name"], "alice_dkv")
        self.assertEqual(account_payload["source_settings"]["username"], "***")
        self.assertEqual(account_payload["source_settings"]["otp_type"], "SMS")
        self.assertEqual(status["active_polls"][0]["account"], "alice_dkv")
        self.assertIn("dkv", api_server.available_source_names())
        self.assertIn("folder", api_server.available_output_names())

    def test_api_lists_folder_output_documents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            download_dir = root / "downloads" / "alice_dkv"
            download_dir.mkdir(parents=True)
            (download_dir / "refund.pdf").write_bytes(b"%PDF")
            account = SourceAccountConfig(
                name="alice_dkv",
                source="dkv",
                maximum_document_mb=100,
                runtime_dir=root / "accounts" / "alice_dkv",
                state_file=root / "accounts" / "alice_dkv" / "state.json",
                lock_file=root / "accounts" / "alice_dkv" / ".run.lock",
                output_configs=(
                    OutputConfig(
                        "local",
                        "folder",
                        {"directory": str(download_dir)},
                    ),
                ),
            )
            documents = api_server.list_downloaded_documents(
                [account], "alice_dkv", 50
            )

        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["account"], "alice_dkv")
        self.assertEqual(documents[0]["source"], "dkv")
        self.assertEqual(documents[0]["filename"], "refund.pdf")
        self.assertEqual(documents[0]["size_bytes"], 4)
        self.assertIn("modified_at", documents[0])

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

    def test_batch_output_hooks_wrap_poll_and_finalize_before_checkpoint(self) -> None:
        register_output("batch_recording", BatchRecordingOutput)
        BatchRecordingOutput.events = []
        BatchRecordingOutput.delivered = []
        BatchRecordingOutput.result = None
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            account = fake_source_account(
                root,
                (OutputConfig("zipper", "batch_recording"),),
            )

            with patch("builtins.print"):
                count = watcher_core.run_poll(account, TwoMessageSource())

            state = watcher_core.load_state(account)

        self.assertEqual(count, 2)
        self.assertEqual(
            BatchRecordingOutput.events,
            ["begin", "deliver:1", "deliver:2", "end", "close"],
        )
        self.assertEqual(BatchRecordingOutput.delivered, ["content-1", "content-2"])
        self.assertEqual(state["seen_ids"], ["1", "2"])
        self.assertIsNotNone(BatchRecordingOutput.result)
        self.assertEqual(BatchRecordingOutput.result.processed_messages, 2)
        self.assertEqual(BatchRecordingOutput.result.delivered_documents, 2)
        self.assertEqual(BatchRecordingOutput.result.failed_messages, 0)

    def test_batch_output_end_failure_prevents_checkpoint(self) -> None:
        register_output("batch_failing", BatchFailingOutput)
        BatchFailingOutput.events = []
        BatchFailingOutput.delivered = []
        BatchFailingOutput.result = None
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            account = fake_source_account(
                root,
                (OutputConfig("zipper", "batch_failing"),),
            )

            with patch("builtins.print"):
                with self.assertRaises(OutputError):
                    watcher_core.run_poll(account, TwoMessageSource())

            self.assertFalse(account.state_file.exists())

        self.assertEqual(
            BatchFailingOutput.events,
            ["begin", "deliver:1", "deliver:2", "end", "close"],
        )

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
            patch("builtins.print"),
        ):
            provider = FakeConfigProvider()
            self.assertEqual(mqtt_trigger.main(provider), 1)
            self.assertTrue(provider.loaded)

    def test_mqtt_publish_status_sends_json_to_status_topic(self) -> None:
        client = FakeMqttStatusClient()
        event = {
            "schema": mqtt_trigger.STATUS_SCHEMA,
            "event": "poll.finished",
            "status": "ok",
            "timestamp": "2026-09-05T12:00:00Z",
            "account": "fede_dkv",
            "source": "dkv",
            "new_messages": 3,
        }

        with patch.dict(
            "os.environ", {"DOCUMENT_MQTT_STATUS_TOPIC": "documents/poll/status"}
        ):
            mqtt_trigger.publish_status(client, event)

        self.assertEqual(len(client.published), 1)
        topic, payload = client.published[0]
        self.assertEqual(topic, "documents/poll/status")
        decoded = json.loads(payload)
        self.assertEqual(decoded["schema"], mqtt_trigger.STATUS_SCHEMA)
        self.assertEqual(decoded["event"], "poll.finished")
        self.assertEqual(decoded["status"], "ok")
        self.assertEqual(decoded["account"], "fede_dkv")
        self.assertEqual(decoded["source"], "dkv")
        self.assertEqual(decoded["new_messages"], 3)

    def test_mqtt_worker_publishes_json_poll_success_status(self) -> None:
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
            jobs: "queue.Queue[SourceAccountConfig | None]" = queue.Queue()
            broker = PushInputBroker()
            client = FakeMqttStatusClient()
            shutdown_event = threading.Event()
            worker_state = mqtt_trigger.WorkerState()
            state = RuntimeState()

            worker_thread = threading.Thread(
                target=mqtt_trigger.worker,
                args=(client, jobs, broker, shutdown_event, worker_state, state),
                daemon=True,
            )
            with (
                patch.object(mqtt_trigger, "poll_account", return_value=7),
                patch.dict(
                    "os.environ",
                    {"DOCUMENT_MQTT_STATUS_TOPIC": "documents/poll/status"},
                ),
                patch("builtins.print"),
            ):
                worker_thread.start()
                jobs.put(account)
                jobs.join()
                jobs.put(None)
                jobs.join()

        payloads = [json.loads(payload) for _, payload in client.published]
        self.assertEqual(
            [(payload["event"], payload["status"]) for payload in payloads],
            [("poll.started", "running"), ("poll.finished", "ok")],
        )
        self.assertEqual(payloads[-1]["account"], "alice_myguichet")
        self.assertEqual(payloads[-1]["source"], "myguichet")
        self.assertEqual(payloads[-1]["new_messages"], 7)
        self.assertEqual(state.snapshot()["recent_events"][-1]["status"], "ok")

    def test_mqtt_worker_publishes_json_poll_error_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            account = SourceAccountConfig(
                name="alice_dkv",
                source="dkv",
                maximum_document_mb=100,
                runtime_dir=root,
                state_file=root / "state.json",
                lock_file=root / ".run.lock",
            )
            jobs: "queue.Queue[SourceAccountConfig | None]" = queue.Queue()
            broker = PushInputBroker()
            client = FakeMqttStatusClient()
            shutdown_event = threading.Event()
            worker_state = mqtt_trigger.WorkerState()
            state = RuntimeState()

            worker_thread = threading.Thread(
                target=mqtt_trigger.worker,
                args=(client, jobs, broker, shutdown_event, worker_state, state),
                daemon=True,
            )
            with (
                patch.object(
                    mqtt_trigger,
                    "poll_account",
                    side_effect=SourceError("source unavailable"),
                ),
                patch.dict(
                    "os.environ",
                    {"DOCUMENT_MQTT_STATUS_TOPIC": "documents/poll/status"},
                ),
                patch("builtins.print"),
            ):
                worker_thread.start()
                jobs.put(account)
                jobs.join()
                jobs.put(None)
                jobs.join()

        payload = json.loads(client.published[-1][1])
        self.assertEqual(payload["event"], "poll.finished")
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["account"], "alice_dkv")
        self.assertEqual(payload["source"], "dkv")
        self.assertEqual(payload["error_type"], "SourceError")
        self.assertEqual(payload["error_message"], "source unavailable")
        self.assertEqual(state.snapshot()["recent_events"][-1]["status"], "error")

    def test_mqtt_request_shutdown_publishes_stopping_status(self) -> None:
        client = FakeMqttStatusClient()
        broker = PushInputBroker()
        shutdown_event = threading.Event()
        worker_state = mqtt_trigger.WorkerState()
        state = RuntimeState()
        account = SourceAccountConfig(
            name="alice_dkv",
            source="dkv",
            maximum_document_mb=100,
            runtime_dir=Path("/tmp/alice_dkv"),
            state_file=Path("/tmp/alice_dkv/state.json"),
            lock_file=Path("/tmp/alice_dkv/.run.lock"),
        )
        worker_state.start(account)

        with (
            patch.dict(
                "os.environ",
                {"DOCUMENT_MQTT_STATUS_TOPIC": "documents/poll/status"},
            ),
            patch("builtins.print"),
        ):
            mqtt_trigger.request_shutdown(
                client, broker, shutdown_event, worker_state, state, "SIGTERM"
            )

        self.assertTrue(shutdown_event.is_set())
        self.assertFalse(client.disconnected)
        with self.assertRaises(InputUnavailableError):
            broker.provide("alice_dkv", {"code": "123456"})
        payload = json.loads(client.published[-1][1])
        self.assertEqual(payload["event"], "runner.stopping")
        self.assertEqual(payload["status"], "stopping")
        self.assertEqual(payload["signal"], "SIGTERM")
        self.assertEqual(payload["active_polls"], 1)
        self.assertEqual(payload["active_accounts"], ["alice_dkv"])
        self.assertEqual(state.snapshot()["recent_events"][-1]["event"], "runner.stopping")

    def test_mqtt_worker_skips_queued_poll_during_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            account = SourceAccountConfig(
                name="alice_dkv",
                source="dkv",
                maximum_document_mb=100,
                runtime_dir=root,
                state_file=root / "state.json",
                lock_file=root / ".run.lock",
            )
            jobs: "queue.Queue[SourceAccountConfig | None]" = queue.Queue()
            broker = PushInputBroker()
            client = FakeMqttStatusClient()
            shutdown_event = threading.Event()
            shutdown_event.set()
            worker_state = mqtt_trigger.WorkerState()
            state = RuntimeState()

            worker_thread = threading.Thread(
                target=mqtt_trigger.worker,
                args=(client, jobs, broker, shutdown_event, worker_state, state),
                daemon=True,
            )
            with (
                patch.object(mqtt_trigger, "poll_account") as mocked,
                patch.dict(
                    "os.environ",
                    {"DOCUMENT_MQTT_STATUS_TOPIC": "documents/poll/status"},
                ),
                patch("builtins.print"),
            ):
                worker_thread.start()
                jobs.put(account)
                jobs.join()
                jobs.put(None)
                jobs.join()

        mocked.assert_not_called()
        payload = json.loads(client.published[-1][1])
        self.assertEqual(payload["event"], "poll.finished")
        self.assertEqual(payload["status"], "skipped")
        self.assertEqual(payload["account"], "alice_dkv")
        self.assertEqual(payload["source"], "dkv")
        self.assertEqual(payload["error_type"], mqtt_trigger.SHUTDOWN_ERROR_TYPE)
        self.assertEqual(state.snapshot()["recent_events"][-1]["status"], "skipped")

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
            jobs: "queue.Queue[SourceAccountConfig | None]" = queue.Queue()
            broker = PushInputBroker()
            shutdown_event = threading.Event()
            worker_state = mqtt_trigger.WorkerState()
            state = RuntimeState()

            worker_thread = threading.Thread(
                target=mqtt_trigger.worker,
                args=(object(), jobs, broker, shutdown_event, worker_state, state),
                daemon=True,
            )
            with patch.object(mqtt_trigger, "poll_account", return_value=7) as mocked:
                with patch("builtins.print"):
                    worker_thread.start()
                    jobs.put(account)
                    jobs.join()
                    jobs.put(None)
                    jobs.join()

            mocked.assert_called_once_with(account, input_broker=broker)


if __name__ == "__main__":
    unittest.main()
