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
from datetime import date
from pathlib import Path
from unittest.mock import patch


WATCHER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WATCHER_ROOT))

import watcher_core  # noqa: E402
import mqtt_trigger  # noqa: E402
import config  # noqa: E402
import api_server  # noqa: E402
import document_watcher  # noqa: E402
from input_broker import (  # noqa: E402
    CliInputBroker,
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
    collect_available_documents,
    collect_invoice_messages,
    collect_treated_refunds,
    document_from_invoice_detail,
    documents_from_refund_detail,
)
from sources.dkv.client import DkvClient  # noqa: E402
from sources.foyer import FoyerDocumentSource  # noqa: E402
from sources.foyer.config import foyer_account_from_source  # noqa: E402
from sources.foyer.source import collect_foyer_documents  # noqa: E402
from sources.myguichet import (  # noqa: E402
    COMMUNAL_BILLS_PER_PAGE,
    COMMUNAL_BILL_MESSAGE_KIND,
    MyGuichetDocumentSource,
    REQUESTS_PER_PAGE,
    collect_communal_bills,
    collect_unseen_communications,
)
from sources.myguichet import login as myguichet_login  # noqa: E402
from sources.myguichet.config import myguichet_account_from_source  # noqa: E402
from sources.prosyndic import (  # noqa: E402
    ProSyndicDocumentSource,
    collect_documents as collect_prosyndic_documents,
    prosyndic_account_from_source,
)
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
        self.communal_status: dict[str, object] = {
            "data": [{"origin": "VDL", "consentStatus": "ACCEPTED"}],
            "size": 1,
        }
        self.communal_pages: dict[tuple[str, int], dict[str, object]] = {}
        self.requested_communal_pages: list[tuple[str, int, int]] = []
        self.edelivery_details: dict[str, dict[str, object]] = {}
        self.downloaded_documents: list[tuple[str, str]] = []
        self.downloaded_communal_bills: list[tuple[str, str]] = []

    def list_communications(self, page: int, per_page: int) -> dict[str, object]:
        self.requested_pages.append(page)
        self.assert_requested_page_size(per_page)
        return self.pages[page]

    def list_communal_bill_consent_status(self) -> dict[str, object]:
        return self.communal_status

    def list_communal_bills(
        self, backend: str, page_number: int, page_size: int
    ) -> dict[str, object]:
        self.requested_communal_pages.append((backend, page_number, page_size))
        return self.communal_pages.get(
            (backend, page_number),
            {
                "totalCount": 0,
                "pageCount": 0,
                "pageNumber": page_number,
                "items": [],
            },
        )

    def get_edelivery(self, communication_id: str) -> dict[str, object]:
        return self.edelivery_details[communication_id]

    def download_document(self, document_id: str, filename: str) -> FakeResponse:
        self.downloaded_documents.append((document_id, filename))
        return FakeResponse([b"%PDF-1.4\n"])

    def download_communal_bill(self, document_id: str, backend: str) -> FakeResponse:
        self.downloaded_communal_bills.append((document_id, backend))
        return FakeResponse([b"%PDF-1.4\n"])

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
        self.available_documents: list[object] = []
        self.on_demand_documents: list[object] = []
        self.invoice_pages: dict[int, dict[str, object]] = {
            0: {
                "groups": [{"items": []}],
                "pagingInfo": {"limit": 20, "offset": 0, "total": 0},
            }
        }
        self.invoice_details: dict[str, dict[str, object]] = {}
        self.downloaded_documents: list[str] = []

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

    def list_available_documents(self) -> list[object]:
        return self.available_documents

    def list_on_demand_documents(self) -> list[object]:
        return self.on_demand_documents

    def list_invoices(self, page_index: int, limit: int) -> dict[str, object]:
        del limit
        return self.invoice_pages[page_index]

    def get_invoice(self, invoice_id: str) -> dict[str, object]:
        return self.invoice_details[invoice_id]

    def download_document(self, document_id: str) -> FakeResponse:
        self.downloaded_documents.append(document_id)
        return FakeResponse([b"%PDF-1.4\n"])

    def close(self) -> None:
        self.closed = True


class FakeProSyndicClient:
    def __init__(self, pages: dict[tuple[str | None, int], dict[str, object]]) -> None:
        self.pages = pages
        self.requested_pages: list[tuple[str | None, int, int]] = []
        self.logged_in: list[tuple[str, str]] = []
        self.downloaded_documents: list[str] = []
        self.closed = False

    def login(self, username: str, password: str) -> None:
        self.logged_in.append((username, password))

    def list_folder(
        self, folder_id: str | None, page: int, limit: int
    ) -> dict[str, object]:
        self.requested_pages.append((folder_id, page, limit))
        return self.pages[(folder_id, page)]

    def download_document(self, document_id: str) -> FakeResponse:
        self.downloaded_documents.append(document_id)
        return FakeResponse([b"%PDF-1.4\n"])

    def close(self) -> None:
        self.closed = True


class FakeFoyerClient:
    def __init__(
        self, pages: dict[tuple[str, int], dict[str, object]] | None = None
    ) -> None:
        self.pages = pages or {}
        self.requested_pages: list[tuple[str, int, dict[str, str | int]]] = []
        self.logged_in: list[tuple[str, str]] = []
        self.refreshed_tokens: list[str] = []
        self.access_token = ""
        self.downloaded_urls: list[str] = []
        self.closed = False

    def set_access_token(self, access_token: str) -> None:
        self.access_token = access_token

    def login(self, username: str, password: str) -> dict[str, object]:
        self.logged_in.append((username, password))
        return {
            "access_token": "foyer-access-token",
            "refresh_token": "foyer-refresh-token",
            "expires_in": 300,
        }

    def refresh_access_token(self, refresh_token: str) -> dict[str, object]:
        self.refreshed_tokens.append(refresh_token)
        return {
            "access_token": "foyer-refreshed-token",
            "refresh_token": "foyer-refresh-token",
            "expires_in": 300,
        }

    def user_profile(self) -> dict[str, object]:
        return {"meta": {"number": 885719}, "sub": "cli:885719"}

    def list_json(
        self, path: str, params: dict[str, str | int]
    ) -> dict[str, object]:
        page = int(params.get("page[offset]", params.get("page[number]", 0)))
        self.requested_pages.append((path, page, dict(params)))
        if (path, page) in self.pages:
            return self.pages[(path, page)]
        if "page[number]" in params:
            return {"data": [], "meta": {"totalPages": 0}}
        return {
            "data": [],
            "meta": {
                "totalRecords": 0,
                "page": {
                    "offset": page,
                    "limit": int(params.get("page[limit]", 50)),
                },
            },
        }

    def download_url(self, url: str) -> FakeResponse:
        self.downloaded_urls.append(url)
        return FakeResponse([b"%PDF-1.4\n"])

    def close(self) -> None:
        self.closed = True


class FakeHttpResponse:
    def __init__(
        self, status_code: int = 200, payload: object | None = None
    ) -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.payload = payload
        self.closed = False

    def json(self) -> object:
        return self.payload

    def close(self) -> None:
        self.closed = True


class FakeHttpSession:
    def __init__(self, responses: list[FakeHttpResponse] | None = None) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.responses = responses or []

    def get(self, url: str, **kwargs: object) -> FakeHttpResponse:
        self.calls.append((url, kwargs))
        if self.responses:
            return self.responses.pop(0)
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

    def test_myguichet_communal_bills_follow_lazy_loaded_pages(self) -> None:
        client = FakeClient({1: {"nbTotalCommunication": 0, "myCommunicationList": []}})
        client.communal_status = {
            "data": [
                {"origin": "SIGI", "consentStatus": "UNKNOWN"},
                {"origin": "VDL", "consentStatus": "ACCEPTED"},
            ],
            "size": 2,
        }
        client.communal_pages = {
            ("VDL", 1): {
                "totalCount": 3,
                "pageCount": 2,
                "pageNumber": 1,
                "items": [
                    {
                        "id": "bill-new",
                        "creationDate": "2026-09-25T00:00:00Z",
                        "reference": "F90296363",
                        "administrationName": "VDL",
                        "documentType": "Service Sports",
                        "mimeType": "application/pdf",
                    },
                    {
                        "id": "bill-seen",
                        "creationDate": "2025-12-15T00:00:00Z",
                        "reference": "F90236835",
                        "administrationName": "VDL",
                        "documentType": "Bierger-Center",
                        "mimeType": "application/pdf",
                    },
                ],
            },
            ("VDL", 2): {
                "totalCount": 3,
                "pageCount": 2,
                "pageNumber": 2,
                "items": [
                    {
                        "id": "bill-old",
                        "creationDate": "2023-05-25T00:00:00Z",
                        "reference": "ECI93075244",
                        "administrationName": "VDL",
                        "documentType": "Etat Civil",
                        "mimeType": "application/pdf",
                    }
                ],
            },
        }

        bills = collect_communal_bills(
            client,  # type: ignore[arg-type]
            seen={"bill-seen"},
            page_size=10,
        )

        self.assertEqual(
            [(bill_id, backend) for bill_id, _, backend in bills],
            [("bill-old", "VDL"), ("bill-new", "VDL")],
        )
        self.assertEqual(client.requested_communal_pages, [("VDL", 1, 10), ("VDL", 2, 10)])

    def test_myguichet_source_collects_and_downloads_communal_bills(self) -> None:
        fake_client = FakeClient(
            {
                1: {
                    "nbTotalCommunication": 1,
                    "myCommunicationList": [communication(123)],
                },
            }
        )
        fake_client.edelivery_details = {
            "123": {
                "attachmentList": [
                    {
                        "externalDocId": "external-doc-1",
                        "docName": "message.pdf",
                    }
                ]
            }
        }
        fake_client.communal_pages = {
            ("VDL", 1): {
                "totalCount": 1,
                "pageCount": 1,
                "pageNumber": 1,
                "items": [
                    {
                        "id": "cde4ad8d-479f-46e2-8dca-4abac383c1da",
                        "creationDate": "2026-09-25T00:00:00Z",
                        "reference": "F90296363",
                        "administrationName": "VDL",
                        "documentType": "Service Sports",
                        "mimeType": "application/pdf",
                    }
                ],
            }
        }
        account = SourceAccountConfig(
            name="alice_myguichet",
            source="myguichet",
            maximum_document_mb=100,
            runtime_dir=Path("/tmp/alice_myguichet"),
            state_file=Path("/tmp/alice_myguichet/state.json"),
            lock_file=Path("/tmp/alice_myguichet/.run.lock"),
            source_settings={"space_id": "10906"},
        )
        source = MyGuichetDocumentSource()
        source.client = fake_client  # type: ignore[assignment]

        messages = source.collect_unseen(
            account, SourceContext(CliInputBroker()), seen=set()
        )
        bill_message = next(
            message
            for message in messages
            if message.metadata.get("kind") == COMMUNAL_BILL_MESSAGE_KIND
        )
        bill_documents = source.list_documents(
            account, SourceContext(CliInputBroker()), bill_message
        )
        bill_response = source.open_document(
            account, SourceContext(CliInputBroker()), bill_message, bill_documents[0]
        )
        communication_message = next(message for message in messages if message.id == "123")
        communication_documents = source.list_documents(
            account, SourceContext(CliInputBroker()), communication_message
        )

        self.assertEqual([message.id for message in messages], ["123", "cde4ad8d-479f-46e2-8dca-4abac383c1da"])
        self.assertEqual(bill_documents[0].name, "2026-09-25 - VDL - Service Sports - F90296363.pdf")
        self.assertEqual(bill_documents[0].content_type, "application/pdf")
        self.assertEqual(
            fake_client.downloaded_communal_bills,
            [("cde4ad8d-479f-46e2-8dca-4abac383c1da", "VDL")],
        )
        self.assertEqual(communication_documents[0].id, "external-doc-1")
        bill_response.close()

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

    def test_dkv_available_documents_flatten_tax_certificate_categories(self) -> None:
        messages = collect_available_documents(
            [
                {
                    "idCategory": "1",
                    "libelleCategory": "Tax certificates",
                    "logo": "tax_certificate",
                    "subCategories": [
                        {"title": "Tax certificate received 2026", "documents": []},
                        {
                            "title": "Tax certificate received 2025",
                            "documents": [
                                {
                                    "idDocument": "CONT#Clients-624143106-1445889",
                                    "label": "Tax certificate Life",
                                    "logo": "tax_certificate",
                                    "order": 0,
                                }
                            ],
                        },
                        {
                            "title": "Tax certificate received 2024",
                            "documents": [
                                {
                                    "idDocument": "CONT#Clients-624143106-1401096",
                                    "label": "Tax certificate Life",
                                    "logo": "tax_certificate",
                                    "order": 0,
                                }
                            ],
                        },
                    ],
                }
            ],
            seen={"CONT#Clients-624143106-1401096"},
            source_type="available",
        )

        self.assertEqual(
            [message.id for message in messages],
            ["CONT#Clients-624143106-1445889"],
        )
        self.assertEqual(messages[0].metadata["kind"], "available_document")
        self.assertEqual(messages[0].metadata["category"], "Tax certificates")
        self.assertEqual(
            messages[0].metadata["subcategory"], "Tax certificate received 2025"
        )

    def test_dkv_invoice_collection_follows_lazy_loaded_pages(self) -> None:
        fake_client = FakeDkvAuthClient()
        fake_client.invoice_pages = {
            0: {
                "groups": [
                    {
                        "items": [
                            {
                                "id": "INV#new",
                                "label": "Invoice newer",
                                "date": "03.09.2026",
                                "documentAvailable": True,
                            },
                            {
                                "id": "INV#without-document",
                                "label": "Invoice no doc",
                                "documentAvailable": False,
                            },
                        ]
                    }
                ],
                "pagingInfo": {"limit": 2, "offset": 0, "total": 4},
            },
            1: {
                "groups": [
                    {
                        "items": [
                            {
                                "id": "INV#old",
                                "label": "Invoice older",
                                "date": "01.09.2026",
                                "documentAvailable": True,
                            },
                            {
                                "id": "INV#seen",
                                "label": "Invoice seen",
                                "documentAvailable": True,
                            },
                        ]
                    }
                ],
                "pagingInfo": {"limit": 2, "offset": 2, "total": 4},
            },
        }

        messages = collect_invoice_messages(
            fake_client, seen={"INV#seen"}, page_limit=2  # type: ignore[arg-type]
        )

        self.assertEqual([message.id for message in messages], ["INV#old", "INV#new"])
        self.assertEqual(messages[0].metadata["kind"], "invoice")

    def test_dkv_invoice_detail_maps_downloadable_document(self) -> None:
        document = document_from_invoice_detail(
            "INV#1",
            {
                "label": "Invoice September",
                "gedDocumentId": "FACT#2026#1",
            },
        )

        self.assertEqual(document.id, "FACT#2026#1")
        self.assertEqual(document.name, "Invoice September")
        self.assertEqual(document.content_type, "application/pdf")

    def test_dkv_source_collects_available_documents_and_downloads_them(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fake_client = FakeDkvAuthClient()
            fake_client.available_documents = [
                {
                    "idCategory": "1",
                    "libelleCategory": "Tax certificates",
                    "subCategories": [
                        {
                            "title": "Tax certificate received 2025",
                            "documents": [
                                {
                                    "idDocument": "CONT#Clients-624143106-1445889",
                                    "label": "Tax certificate Life",
                                }
                            ],
                        }
                    ],
                }
            ]
            account = SourceAccountConfig(
                name="alice_dkv",
                source="dkv",
                maximum_document_mb=100,
                runtime_dir=root,
                state_file=root / "state.json",
                lock_file=root / ".run.lock",
                source_settings={"username": "alice-user", "password": "secret"},
            )
            source = DkvDocumentSource(lambda: fake_client)  # type: ignore[arg-type]

            messages = source.collect_unseen(
                account, SourceContext(StaticInputBroker({"code": "123456"})), seen=set()
            )
            documents = source.list_documents(
                account, SourceContext(CliInputBroker()), messages[0]
            )
            response = source.open_document(
                account, SourceContext(CliInputBroker()), messages[0], documents[0]
            )

            self.assertEqual([message.id for message in messages], ["CONT#Clients-624143106-1445889"])
            self.assertEqual(documents[0].id, "CONT#Clients-624143106-1445889")
            self.assertEqual(documents[0].name, "Tax certificate Life")
            self.assertEqual(
                fake_client.downloaded_documents,
                ["CONT#Clients-624143106-1445889"],
            )
            response.close()

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

    def test_dkv_client_treats_empty_invoice_tab_as_empty_page(self) -> None:
        client = DkvClient()
        fake_session = FakeHttpSession([FakeHttpResponse(status_code=204)])
        client.session = fake_session  # type: ignore[assignment]

        payload = client.list_invoices(page_index=0, limit=20)

        self.assertEqual(payload["groups"], [])
        self.assertEqual(payload["pagingInfo"], {"limit": 20, "offset": 0, "total": 0})
        self.assertTrue(fake_session.calls[0][0].endswith("/invoices"))
        self.assertEqual(
            fake_session.calls[0][1]["params"],
            {"limit": 20, "pageIndex": 0},
        )

    def test_foyer_config_validates_credentials_and_defaults(self) -> None:
        account = SourceAccountConfig(
            name="alice_foyer",
            source="foyer",
            maximum_document_mb=100,
            runtime_dir=Path("/tmp/alice_foyer"),
            state_file=Path("/tmp/alice_foyer/state.json"),
            lock_file=Path("/tmp/alice_foyer/.run.lock"),
            source_settings={
                "username": "alice",
                "password": "secret",
                "page_limit": "25",
                "lookback_years": "3",
            },
        )

        foyer = foyer_account_from_source(account)

        self.assertEqual(foyer.username, "alice")
        self.assertEqual(foyer.page_limit, 25)
        self.assertEqual(foyer.lookback_years, 3)
        self.assertEqual(foyer.token_file, Path("/tmp/alice_foyer/foyer_token.json"))

    def test_foyer_collects_lazy_loaded_offset_pages_and_dedupes(self) -> None:
        client = FakeFoyerClient(
            {
                (
                    "/v1/contrats/documents",
                    0,
                ): {
                    "data": [
                        {
                            "id": "contract-new",
                            "attributes": {
                                "codeDocument": {"label": "Contract newer"},
                                "dateEmission": "2026-02-01",
                                "url": "https://api.foyer.lu/files/download?token=new",
                            },
                        },
                        {
                            "id": "contract-overlap",
                            "attributes": {
                                "codeDocument": {"label": "Contract overlap"},
                                "dateEmission": "2026-01-15",
                                "url": "https://api.foyer.lu/files/download?token=overlap",
                            },
                        },
                    ],
                    "meta": {
                        "totalRecords": 3,
                        "page": {"offset": 0, "limit": 2},
                    },
                },
                (
                    "/v1/contrats/documents",
                    2,
                ): {
                    "data": [
                        {
                            "id": "contract-overlap",
                            "attributes": {
                                "codeDocument": {"label": "Contract overlap"},
                                "dateEmission": "2026-01-15",
                                "url": "https://api.foyer.lu/files/download?token=overlap",
                            },
                        },
                        {
                            "id": "contract-old",
                            "attributes": {
                                "codeDocument": {"label": "Contract older"},
                                "dateEmission": "2026-01-01",
                                "url": "https://api.foyer.lu/files/download?token=old",
                            },
                        },
                    ],
                    "meta": {
                        "totalRecords": 3,
                        "page": {"offset": 2, "limit": 2},
                    },
                },
            }
        )

        documents = collect_foyer_documents(
            client,  # type: ignore[arg-type]
            client_number="885719",
            lookback=date(2021, 9, 21),
            page_limit=2,
        )

        self.assertEqual(
            [document.id for document in documents],
            ["contract:contract-old", "contract:contract-overlap", "contract:contract-new"],
        )
        self.assertEqual(
            [
                (path, page)
                for path, page, _ in client.requested_pages
                if path == "/v1/contrats/documents"
            ],
            [("/v1/contrats/documents", 0), ("/v1/contrats/documents", 2)],
        )
        invoice_request = next(
            params
            for path, _, params in client.requested_pages
            if path == "/v1/compta/situation-compte/liste-documents-factures"
        )
        self.assertEqual(invoice_request["filter[client]"], "885719")

    def test_foyer_source_maps_documents_and_persists_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fake_client = FakeFoyerClient(
                {
                    (
                        "/v1/compta/situation-compte/liste-documents-factures",
                        0,
                    ): {
                        "data": [
                            {
                                "id": "invoice-new",
                                "attributes": {
                                    "numeroFacture": "F2026-001",
                                    "dateCreation": "2026-02-01",
                                    "href": "https://api.foyer.lu/files/download?token=invoice",
                                },
                            },
                            {
                                "id": "invoice-seen",
                                "attributes": {
                                    "numeroFacture": "F2026-000",
                                    "dateCreation": "2026-01-01",
                                    "href": "https://api.foyer.lu/files/download?token=seen",
                                },
                            },
                        ],
                        "meta": {
                            "totalRecords": 2,
                            "page": {"offset": 0, "limit": 50},
                        },
                    },
                }
            )
            account = SourceAccountConfig(
                name="alice_foyer",
                source="foyer",
                maximum_document_mb=100,
                runtime_dir=root,
                state_file=root / "state.json",
                lock_file=root / ".run.lock",
                source_settings={"username": "alice", "password": "secret"},
            )
            source = FoyerDocumentSource(lambda: fake_client)  # type: ignore[arg-type]

            messages = source.collect_unseen(
                account, SourceContext(CliInputBroker()), seen={"invoice:invoice-seen"}
            )
            documents = source.list_documents(
                account, SourceContext(CliInputBroker()), messages[0]
            )
            response = source.open_document(
                account, SourceContext(CliInputBroker()), messages[0], documents[0]
            )

            token_file = root / "foyer_token.json"
            self.assertEqual([message.id for message in messages], ["invoice:invoice-new"])
            self.assertEqual(documents[0].name, "F2026-001")
            self.assertEqual(documents[0].content_type, "application/pdf")
            self.assertEqual(fake_client.logged_in, [("alice", "secret")])
            self.assertEqual(
                fake_client.downloaded_urls,
                ["https://api.foyer.lu/files/download?token=invoice"],
            )
            self.assertTrue(token_file.exists())
            self.assertIn("foyer-access-token", token_file.read_text(encoding="utf-8"))
            response.close()

    def test_prosyndic_config_requires_https_base_url(self) -> None:
        account = SourceAccountConfig(
            name="alice_prosyndic",
            source="prosyndic",
            maximum_document_mb=100,
            runtime_dir=Path("/tmp/alice_prosyndic"),
            state_file=Path("/tmp/alice_prosyndic/state.json"),
            lock_file=Path("/tmp/alice_prosyndic/.run.lock"),
            source_settings={
                "base_url": "https://tenant.prosyndic-delta.lu",
                "username": "alice",
                "password": "secret",
                "page_limit": "50",
            },
        )

        prosyndic = prosyndic_account_from_source(account)

        self.assertEqual(prosyndic.base_url, "https://tenant.prosyndic-delta.lu")
        self.assertEqual(prosyndic.page_limit, 50)

    def test_prosyndic_traverses_nested_paginated_folders(self) -> None:
        client = FakeProSyndicClient(
            {
                (None, 1): {
                    "data": {
                        "Classeurs": [
                            {"id": "10", "nom": "Root folder", "parent": None}
                        ],
                        "Documents": [],
                    },
                    "pages": {"current": 1, "last": 1},
                },
                ("10", 1): {
                    "data": {
                        "Classeurs": [
                            {"id": "11", "nom": "Nested", "parent": "10"}
                        ],
                        "Documents": [
                            {
                                "id": "doc-newer",
                                "title": "Newer.pdf",
                                "mime_type": "application/pdf",
                                "date_commit": "2026-02-01 10:00:00",
                            }
                        ],
                    },
                    "pages": {"current": 1, "last": 2},
                },
                ("10", 2): {
                    "data": {
                        "Classeurs": [],
                        "Documents": [
                            {
                                "id": "doc-older",
                                "title": "Older.pdf",
                                "mime_type": "application/pdf",
                                "date_commit": "2026-01-01 10:00:00",
                            }
                        ],
                    },
                    "pages": {"current": 2, "last": 2},
                },
                ("11", 1): {
                    "data": {
                        "Classeurs": [],
                        "Documents": [
                            {
                                "id": "doc-nested",
                                "title": "Nested",
                                "mime_type": "application/pdf",
                                "date_commit": "2026-03-01 10:00:00",
                            }
                        ],
                    },
                    "pages": {"current": 1, "last": 1},
                },
            }
        )

        documents = collect_prosyndic_documents(client, page_limit=50)  # type: ignore[arg-type]

        self.assertEqual(
            [document.id for document in documents],
            ["doc-older", "doc-newer", "doc-nested"],
        )
        self.assertEqual(documents[2].folder_path, ("Root folder", "Nested"))
        self.assertEqual(
            client.requested_pages,
            [(None, 1, 50), ("10", 1, 50), ("10", 2, 50), ("11", 1, 50)],
        )

    def test_prosyndic_source_maps_each_remote_document_as_one_message(self) -> None:
        fake_client = FakeProSyndicClient(
            {
                (None, 1): {
                    "data": {
                        "Classeurs": [],
                        "Documents": [
                            {
                                "id": "56792",
                                "title": "DARWIN - PV AG 2026 - Signé.pdf",
                                "mime_type": "application/pdf",
                                "date_commit": "2026-06-19 08:48:25",
                            },
                            {
                                "id": "44836",
                                "title": "Already seen.pdf",
                                "mime_type": "application/pdf",
                                "date_commit": "2026-01-01 00:00:00",
                            },
                        ],
                    },
                    "pages": {"current": 1, "last": 1},
                },
            }
        )
        account = SourceAccountConfig(
            name="alice_prosyndic",
            source="prosyndic",
            maximum_document_mb=100,
            runtime_dir=Path("/tmp/alice_prosyndic"),
            state_file=Path("/tmp/alice_prosyndic/state.json"),
            lock_file=Path("/tmp/alice_prosyndic/.run.lock"),
            source_settings={
                "base_url": "https://tenant.prosyndic-delta.lu",
                "username": "alice",
                "password": "secret",
            },
        )
        source = ProSyndicDocumentSource(lambda base_url: fake_client)  # type: ignore[arg-type]

        messages = source.collect_unseen(
            account, SourceContext(CliInputBroker()), seen={"44836"}
        )
        documents = source.list_documents(
            account, SourceContext(CliInputBroker()), messages[0]
        )
        response = source.open_document(
            account, SourceContext(CliInputBroker()), messages[0], documents[0]
        )

        self.assertEqual([message.id for message in messages], ["56792"])
        self.assertEqual(documents[0].id, "56792")
        self.assertEqual(documents[0].name, "DARWIN - PV AG 2026 - Signé.pdf")
        self.assertEqual(documents[0].content_type, "application/pdf")
        self.assertEqual(fake_client.logged_in, [("alice", "secret")])
        self.assertEqual(fake_client.downloaded_documents, ["56792"])
        response.close()

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

    def test_push_input_broker_notifies_when_input_is_requested(self) -> None:
        requested: "queue.Queue[InputChallenge]" = queue.Queue()
        broker = PushInputBroker(
            early_answer_ttl_seconds=10,
            on_input_requested=requested.put,
        )

        def wait_for_input() -> None:
            broker.request_input(
                InputChallenge(
                    account_name="alice_dkv",
                    source="dkv",
                    kind="otp",
                    prompt="Enter the SMS code",
                    fields=("code",),
                    timeout_seconds=2,
                    id="challenge-1",
                )
            )

        with patch("builtins.print"):
            thread = threading.Thread(target=wait_for_input)
            thread.start()
            challenge = requested.get(timeout=1)
            broker.provide("alice_dkv", {"code": "123456"})
            thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(challenge.account_name, "alice_dkv")
        self.assertEqual(challenge.source, "dkv")
        self.assertEqual(challenge.id, "challenge-1")

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

    def test_env_config_provider_normalizes_compose_quoted_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            provider = config.EnvConfigProvider(
                environ={
                    "DOCUMENT_USERS": "alice",
                    "DOCUMENT_ALICE_MYGUICHET_LUXTRUST_USERNAME": "'alice-user'",
                    "DOCUMENT_ALICE_MYGUICHET_LUXTRUST_PASSWORD": '"secret"',
                    "DOCUMENT_ALICE_MYGUICHET_SPACE_ID": "123",
                    "DOCUMENT_ALICE_MYGUICHET_OUTPUT_FOLDER_DIRECTORY": '"downloads/alice docs"',
                },
                root=root,
            )

            account = provider.get_source_account("alice_myguichet")

        self.assertEqual(account.source_settings["luxtrust_username"], "alice-user")
        self.assertEqual(account.source_settings["luxtrust_password"], "secret")
        self.assertEqual(
            account.output_configs[0].settings["directory"], "downloads/alice docs"
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
        styles = (api_server.DASHBOARD_DIR / "styles.css").read_text(
            encoding="utf-8"
        )

        self.assertIn('href="/assets/styles.css"', index)
        self.assertIn('src="/assets/app.js"', index)
        self.assertNotIn("<style>", index)
        self.assertNotIn("<script>", index)
        self.assertIn('id="serviceBadges"', index)
        self.assertIn('class="service-tab is-selected"', index)
        self.assertIn('id="logLiveState"', index)
        self.assertIn('id="themeSelect"', index)
        self.assertIn('<option value="modern">Modern</option>', index)
        self.assertIn('<option value="cyber">Cyber</option>', index)
        self.assertIn('<option value="cli">CLI</option>', index)
        self.assertIn('data-log-filter="error"', index)
        self.assertIn("Source Documents Last Crawl", index)
        self.assertIn("Last Crawl Source Documents", index)
        self.assertIn('id="documentsTitle"', index)
        self.assertIn("<th>Poll</th>", index)
        self.assertIn("<th>Details</th>", index)
        self.assertNotIn('id="outputConfig"', index)
        self.assertIn("const $ = (id) => document.getElementById(id);", javascript)
        self.assertIn("API error:", javascript)
        self.assertIn("async function postJson(url, payload = null)", javascript)
        self.assertIn("async function triggerPoll(accountName, mode = \"normal\")", javascript)
        self.assertIn("async function clearSeen(accountName)", javascript)
        self.assertIn("async function unseeDocument(accountName, messageId)", javascript)
        self.assertIn('const THEME_STORAGE_KEY = "documentWatcherTheme"', javascript)
        self.assertIn(
            'new Set(["default", "modern", "cyber", "cli", "win95"])',
            javascript,
        )
        self.assertIn("function normalizeTheme(value)", javascript)
        self.assertIn("function saveTheme(theme)", javascript)
        self.assertIn("function renderOutputPills(outputs)", javascript)
        self.assertIn("function renderAccountDetailRow(account)", javascript)
        self.assertIn("function lastPollFrom(status)", javascript)
        self.assertIn("function renderEventDetails(event)", javascript)
        self.assertIn('html[data-theme="win95"]', styles)
        self.assertIn('html[data-theme="modern"]', styles)
        self.assertIn('html[data-theme="cyber"]', styles)
        self.assertIn('html[data-theme="cli"]', styles)
        self.assertIn('html[data-theme="cli"] body {\n  background: #050505;', styles)
        self.assertIn('html[data-theme="cli"] body::before {\n  content: none;', styles)
        self.assertIn(".header-ribbon {\n  display: none;", styles)
        self.assertIn('html[data-theme="win95"] .header-ribbon {\n  display: flex;', styles)
        self.assertIn(".panel > h2::after", styles)
        self.assertIn("width: 100% !important;", styles)
        self.assertIn("status.last_poll", javascript)
        self.assertIn("lastPoll.documents", javascript)
        self.assertIn("lastPoll.new_documents", javascript)
        self.assertIn('class="state-trigger clear-seen"', javascript)
        self.assertIn('class="poll-trigger checkpoint-trigger"', javascript)
        self.assertIn('data-mode="checkpoint"', javascript)
        self.assertIn('title="Clear all seen messages for this source"', javascript)
        self.assertIn('class="state-trigger unsee-document"', javascript)
        self.assertNotIn('fetchJson("/api/documents?limit=20")', javascript)
        self.assertIn("function connectEventStream()", javascript)
        self.assertIn("new EventSource", javascript)
        self.assertIn('class="poll-trigger"', javascript)
        self.assertIn("fetchJson(\"/api/service\")", javascript)
        self.assertIn('class="config-list"', javascript)
        self.assertIn('colspan="7"', javascript)
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

    def test_api_mqtt_poll_trigger_publishes_account_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            account = fake_source_account(Path(temporary_directory))
            state = RuntimeState()
            published: dict[str, object] = {}

            def publisher(**kwargs: object) -> None:
                published.update(kwargs)

            result = api_server.publish_mqtt_poll_trigger(
                account,
                state,
                environ={
                    "DOCUMENT_RUN_MODE": "watcher",
                    "DOCUMENT_MQTT_HOST": "mqtt.local",
                    "DOCUMENT_MQTT_PORT": "1884",
                    "DOCUMENT_MQTT_TOPIC": "documents/poll",
                    "DOCUMENT_MQTT_USERNAME": "home_iot",
                    "DOCUMENT_MQTT_PASSWORD": "secret",
                },
                publisher=publisher,
            )

        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["account"], account.name)
        self.assertEqual(result["mode"], "normal")
        self.assertEqual(published["topic"], "documents/poll")
        self.assertEqual(published["payload"], '{"account":"alice","mode":"normal"}')
        self.assertEqual(published["hostname"], "mqtt.local")
        self.assertEqual(published["port"], 1884)
        self.assertEqual(
            published["auth"],
            {"username": "home_iot", "password": "secret"},
        )
        events = state.recent_events()
        self.assertEqual(events[0]["event"], "trigger.published")
        self.assertEqual(events[0]["status"], "queued")

    def test_api_mqtt_poll_trigger_requires_running_mqtt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            account = fake_source_account(Path(temporary_directory))
            state = RuntimeState()

            with self.assertRaises(api_server.MqttTriggerUnavailable):
                api_server.publish_mqtt_poll_trigger(
                    account,
                    state,
                    environ={
                        "DOCUMENT_RUN_MODE": "api",
                        "DOCUMENT_MQTT_HOST": "mqtt.local",
                    },
                    publisher=lambda **kwargs: None,
                )

        events = state.recent_events()
        self.assertEqual(events[0]["event"], "trigger.rejected")
        self.assertEqual(events[0]["status"], "error")

    def test_api_mqtt_poll_trigger_can_publish_checkpoint_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            account = fake_source_account(Path(temporary_directory))
            state = RuntimeState()
            published: dict[str, object] = {}

            result = api_server.publish_mqtt_poll_trigger(
                account,
                state,
                mode="checkpoint",
                environ={
                    "DOCUMENT_RUN_MODE": "watcher",
                    "DOCUMENT_MQTT_HOST": "mqtt.local",
                },
                publisher=lambda **kwargs: published.update(kwargs),
            )

        self.assertEqual(result["mode"], "checkpoint")
        self.assertEqual(
            published["payload"], '{"account":"alice","mode":"checkpoint"}'
        )

    def test_runtime_state_assigns_event_ids_and_recent_events(self) -> None:
        state = RuntimeState(max_events=2)
        state.record_event("first", "ok")
        state.record_event("second", "error", message="failed")
        state.record_event("third", "ok")

        events = state.recent_events()

        self.assertEqual([event["event"] for event in events], ["second", "third"])
        self.assertEqual([event["id"] for event in events], [2, 3])
        self.assertEqual(state.latest_event_id(), 3)
        self.assertEqual(state.wait_for_events(2, timeout_seconds=0.01)[0]["event"], "third")

    def test_runtime_state_tracks_last_poll_result(self) -> None:
        state = RuntimeState()

        state.poll_started("alice_dkv", "dkv")
        state.document_delivered(
            "alice_dkv",
            "dkv",
            message_id="message-1",
            document_id="document-1",
            filename="refund.pdf",
            content_type="application/pdf",
            size_bytes=4,
        )
        state.poll_finished("alice_dkv", "dkv", "ok", new_messages=104)

        status = state.snapshot()
        event = status["recent_events"][-1]

        self.assertEqual(status["active_polls"], [])
        self.assertEqual(status["last_poll"]["account"], "alice_dkv")
        self.assertEqual(status["last_poll"]["source"], "dkv")
        self.assertEqual(status["last_poll"]["status"], "ok")
        self.assertEqual(status["last_poll"]["new_messages"], 104)
        self.assertEqual(status["last_poll"]["new_documents"], 1)
        self.assertEqual(status["last_poll"]["documents"][0]["filename"], "refund.pdf")
        self.assertEqual(event["message"], "1 new document(s) in 104 message(s)")
        self.assertEqual(event["details"]["new_messages"], 104)
        self.assertEqual(event["details"]["new_documents"], 1)

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
        self.assertIn("/api/accounts/{account_name}/poll", openapi_paths)
        self.assertIn("/api/accounts/{account_name}/seen/clear", openapi_paths)
        self.assertIn("/api/accounts/{account_name}/seen/unsee", openapi_paths)
        self.assertIn("/api/status", openapi_paths)
        self.assertIn("/api/events", openapi_paths)
        self.assertIn("/api/events/stream", openapi_paths)
        self.assertIn("/api/service", openapi_paths)
        self.assertIn("/", route_paths)
        self.assertIn("/assets", route_paths)
        self.assertEqual(account_payload["name"], "alice_dkv")
        self.assertEqual(account_payload["source_settings"]["username"], "***")
        self.assertEqual(account_payload["source_settings"]["otp_type"], "SMS")
        self.assertEqual(status["active_polls"][0]["account"], "alice_dkv")
        self.assertIn("dkv", api_server.available_source_names())
        self.assertIn("folder", api_server.available_output_names())

    def test_api_status_uses_latest_persisted_last_poll(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            account = SourceAccountConfig(
                name="alice_dkv",
                source="dkv",
                maximum_document_mb=100,
                runtime_dir=root / "accounts" / "alice_dkv",
                state_file=root / "accounts" / "alice_dkv" / "state.json",
                lock_file=root / "accounts" / "alice_dkv" / ".run.lock",
            )
            account.state_file.parent.mkdir(parents=True)
            account.state_file.write_text(
                json.dumps(
                    {
                        "seen_ids": ["1"],
                        "last_run": "2026-09-09T10:00:00+00:00",
                        "last_poll": {
                            "account": "alice_dkv",
                            "source": "dkv",
                            "status": "ok",
                            "finished_at": "2026-09-09T10:00:00+00:00",
                            "new_messages": 1,
                            "new_documents": 2,
                            "failed_messages": 0,
                            "documents": [
                                {
                                    "account": "alice_dkv",
                                    "source": "dkv",
                                    "message_id": "1",
                                    "document_id": "a",
                                    "filename": "a.pdf",
                                    "content_type": "application/pdf",
                                    "size_bytes": 4,
                                },
                                {
                                    "account": "alice_dkv",
                                    "source": "dkv",
                                    "message_id": "1",
                                    "document_id": "b",
                                    "filename": "b.pdf",
                                    "content_type": "application/pdf",
                                    "size_bytes": 5,
                                },
                            ],
                        },
                    }
                ),
                encoding="utf-8",
            )

            status = api_server.merge_status_with_persisted_poll(
                RuntimeState().snapshot(), [account]
            )

        self.assertEqual(status["last_poll"]["new_documents"], 2)
        self.assertTrue(status["last_poll"]["documents"][0]["seen"])
        self.assertEqual(
            [document["filename"] for document in status["last_poll"]["documents"]],
            ["a.pdf", "b.pdf"],
        )

    def test_seen_state_can_be_cleared_per_account(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            account = fake_source_account(root)
            account.state_file.write_text(
                json.dumps({"seen_ids": ["1", "2"], "last_run": "now"}),
                encoding="utf-8",
            )

            result = watcher_core.clear_seen_messages(account)
            state = watcher_core.load_state(account)

        self.assertEqual(result["removed_messages"], 2)
        self.assertEqual(result["seen_count"], 0)
        self.assertEqual(state["seen_ids"], [])

    def test_seen_state_can_unsee_one_message(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            account = fake_source_account(root)
            account.state_file.write_text(
                json.dumps({"seen_ids": ["1", "2"], "last_run": "now"}),
                encoding="utf-8",
            )

            result = watcher_core.unsee_message(account, "1")
            state = watcher_core.load_state(account)

        self.assertTrue(result["removed"])
        self.assertEqual(result["message_id"], "1")
        self.assertEqual(result["seen_count"], 1)
        self.assertEqual(state["seen_ids"], ["2"])

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

    def test_env_config_provider_supports_output_type_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment = {
                "DOCUMENT_USERS": "alice",
                "DOCUMENT_ALICE_SOURCES": "taxbox",
                "DOCUMENT_OUTPUT_FOLDER_FILE_MODE": "0664",
                "DOCUMENT_OUTPUT_FOLDER_DIR_MODE": "0775",
                "DOCUMENT_ALICE_TAXBOX_OUTPUTS": "local:folder,paperless:folder",
                "DOCUMENT_ALICE_TAXBOX_OUTPUT_LOCAL_DIRECTORY": str(root / "local"),
                "DOCUMENT_ALICE_TAXBOX_OUTPUT_PAPERLESS_DIRECTORY": str(
                    root / "paperless"
                ),
                "DOCUMENT_ALICE_TAXBOX_OUTPUT_PAPERLESS_FILE_MODE": "0640",
            }
            provider = config.EnvConfigProvider(environ=environment, root=root)

            account = provider.get_source_account("alice:taxbox")

        local, paperless = account.output_configs
        self.assertEqual(local.settings["file_mode"], "0664")
        self.assertEqual(local.settings["dir_mode"], "0775")
        self.assertEqual(local.settings["directory"], str(root / "local"))
        self.assertEqual(paperless.settings["file_mode"], "0640")
        self.assertEqual(paperless.settings["dir_mode"], "0775")
        self.assertEqual(paperless.settings["directory"], str(root / "paperless"))

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

    def test_myguichet_headless_defaults_to_true(self) -> None:
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
                },
            )

            myguichet = myguichet_account_from_source(account)

            self.assertTrue(myguichet.headless)

    def test_myguichet_detects_rejected_luxtrust_credentials_text(self) -> None:
        self.assertTrue(
            myguichet_login._looks_like_rejected_credentials(
                "Some data you have entered are incorrect. Please try again."
            )
        )
        self.assertFalse(
            myguichet_login._looks_like_rejected_credentials(
                "Approve the request on your mobile device."
            )
        )

    def test_myguichet_session_cookie_timeout_is_actionable(self) -> None:
        class Context:
            def cookies(self) -> list[dict[str, str]]:
                return []

        with (
            patch.object(myguichet_login.time, "monotonic", side_effect=[0.0, 2.0]),
            patch.object(myguichet_login.time, "sleep"),
            self.assertRaisesRegex(
                myguichet_login.LoginError,
                "Timed out after 1s waiting for LuxTrust approval",
            ),
        ):
            myguichet_login.wait_for_session_cookie(Context(), "fr", 1)

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
        self.assertEqual(state["last_poll"]["status"], "ok")
        self.assertEqual(state["last_poll"]["new_messages"], 2)
        self.assertEqual(state["last_poll"]["new_documents"], 2)
        self.assertEqual(
            [document["filename"] for document in state["last_poll"]["documents"]],
            ["1_doc-1_document-1.txt", "2_doc-2_document-2.txt"],
        )
        self.assertIsNotNone(BatchRecordingOutput.result)
        self.assertEqual(BatchRecordingOutput.result.processed_messages, 2)
        self.assertEqual(BatchRecordingOutput.result.delivered_documents, 2)
        self.assertEqual(BatchRecordingOutput.result.failed_messages, 0)

    def test_checkpoint_poll_marks_messages_seen_without_outputs_or_downloads(self) -> None:
        register_output("batch_recording", BatchRecordingOutput)
        BatchRecordingOutput.events = []
        BatchRecordingOutput.delivered = []
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            account = fake_source_account(
                root,
                (OutputConfig("zipper", "batch_recording"),),
            )

            with patch("builtins.print"):
                count = watcher_core.run_poll(
                    account,
                    TwoMessageSource(),
                    mode=watcher_core.PollMode.CHECKPOINT,
                )

            state = watcher_core.load_state(account)

        self.assertEqual(count, 2)
        self.assertEqual(state["seen_ids"], ["1", "2"])
        self.assertEqual(state["last_poll"]["mode"], "checkpoint")
        self.assertEqual(state["last_poll"]["new_messages"], 2)
        self.assertEqual(state["last_poll"]["new_documents"], 0)
        self.assertEqual(state["last_poll"]["documents"], [])
        self.assertEqual(BatchRecordingOutput.events, [])
        self.assertEqual(BatchRecordingOutput.delivered, [])

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
            runtime = RuntimeState()
            context = SourceContext(CliInputBroker(), runtime)

            with patch("builtins.print"):
                with self.assertRaises(SourceError):
                    watcher_core.run_poll(account, PartlyFailingSource(), context)

            state = watcher_core.load_state(account)
            self.assertEqual(state["seen_ids"], ["2"])
            downloaded = list((root / "downloads").iterdir())
            self.assertEqual(len(downloaded), 1)
            self.assertEqual(downloaded[0].read_bytes(), b"downloaded")
            events = runtime.recent_events()
            self.assertEqual(events[0]["event"], "message.failed")
            self.assertEqual(events[0]["status"], "error")
            self.assertEqual(events[0]["account"], account.name)
            self.assertEqual(events[0]["source"], account.source)
            self.assertEqual(events[0]["details"]["message_id"], "1")
            self.assertEqual(
                events[0]["details"]["error_type"],
                "SourceResponseError",
            )

    def test_cli_records_account_level_poll_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            account = fake_source_account(Path(temporary_directory))
            provider = FakeConfigProvider([account])
            runtime = RuntimeState()

            with (
                patch.object(document_watcher, "runtime_state", runtime),
                patch.object(
                    document_watcher,
                    "install_shutdown_handlers",
                    return_value=(None, None),
                ),
                patch.object(
                    document_watcher,
                    "poll_account",
                    side_effect=SourceError("source unavailable"),
                ),
                patch("builtins.print"),
            ):
                exit_code = document_watcher.main(
                    ["--account", account.name],
                    provider,
                )

            self.assertEqual(exit_code, 1)
            events = runtime.recent_events()
            self.assertEqual([event["event"] for event in events], ["poll.started", "poll.finished"])
            self.assertEqual(events[1]["status"], "error")
            self.assertEqual(events[1]["message"], "source unavailable")
            self.assertEqual(events[1]["details"]["error_type"], "SourceError")

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
        request = mqtt_trigger.parse_trigger_payload(
            '{"account":"alice","mode":"checkpoint"}'
        )
        self.assertEqual(request.account_names, ["alice"])
        self.assertEqual(request.mode, watcher_core.PollMode.CHECKPOINT)

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

    def test_mqtt_input_requested_status_event_is_generic(self) -> None:
        event = mqtt_trigger.input_requested_status_event(
            InputChallenge(
                account_name="alice_dkv",
                source="dkv",
                kind="otp",
                prompt="Enter the SMS code",
                fields=("code",),
                timeout_seconds=300,
                id="challenge-1",
            )
        )

        self.assertEqual(event["event"], "input.requested")
        self.assertEqual(event["status"], "waiting")
        self.assertEqual(event["account"], "alice_dkv")
        self.assertEqual(event["source"], "dkv")
        self.assertEqual(event["kind"], "otp")
        self.assertEqual(event["prompt"], "Enter the SMS code")
        self.assertEqual(event["fields"], ["code"])
        self.assertEqual(event["timeout_seconds"], 300)
        self.assertEqual(event["challenge_id"], "challenge-1")

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
            jobs: "queue.Queue[mqtt_trigger.PollJob | None]" = queue.Queue()
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
                jobs.put(mqtt_trigger.PollJob(account, watcher_core.PollMode.NORMAL))
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
        self.assertEqual(payloads[-1]["mode"], "normal")
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
            jobs: "queue.Queue[mqtt_trigger.PollJob | None]" = queue.Queue()
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
                jobs.put(mqtt_trigger.PollJob(account, watcher_core.PollMode.NORMAL))
                jobs.join()
                jobs.put(None)
                jobs.join()

        payload = json.loads(client.published[-1][1])
        self.assertEqual(payload["event"], "poll.finished")
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["account"], "alice_dkv")
        self.assertEqual(payload["source"], "dkv")
        self.assertEqual(payload["mode"], "normal")
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
            jobs: "queue.Queue[mqtt_trigger.PollJob | None]" = queue.Queue()
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
                jobs.put(mqtt_trigger.PollJob(account, watcher_core.PollMode.CHECKPOINT))
                jobs.join()
                jobs.put(None)
                jobs.join()

        mocked.assert_not_called()
        payload = json.loads(client.published[-1][1])
        self.assertEqual(payload["event"], "poll.finished")
        self.assertEqual(payload["status"], "skipped")
        self.assertEqual(payload["account"], "alice_dkv")
        self.assertEqual(payload["source"], "dkv")
        self.assertEqual(payload["mode"], "checkpoint")
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
            jobs: "queue.Queue[mqtt_trigger.PollJob | None]" = queue.Queue()
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
                    jobs.put(mqtt_trigger.PollJob(account, watcher_core.PollMode.CHECKPOINT))
                    jobs.join()
                    jobs.put(None)
                    jobs.join()

            mocked.assert_called_once_with(
                account,
                input_broker=broker,
                runtime_state=state,
                mode=watcher_core.PollMode.CHECKPOINT,
            )


if __name__ == "__main__":
    unittest.main()
