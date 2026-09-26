"""MyGuichet document source adapter."""

from __future__ import annotations

from typing import Any

from sources.base import (
    DownloadResponse,
    SourceAccountConfig,
    SourceContext,
    SourceDocument,
    SourceError,
    SourceMessage,
    SourceResponseError,
    SourceSessionExpired,
)
from storage import restrict_file

from sources.myguichet.client import (
    MyGuichetClient,
    MyGuichetError,
    PortalResponseError,
    SessionExpired,
)
from sources.myguichet.config import (
    MyGuichetAccountConfig,
    myguichet_account_from_source,
)


REQUESTS_PER_PAGE = 100
COMMUNAL_BILLS_PER_PAGE = 100
COMMUNAL_BILL_MESSAGE_KIND = "communal_bill"
DEFAULT_COMMUNAL_BILL_BACKEND = "VDL"


def _first_text(metadata: dict[str, Any], *keys: str) -> object:
    for key in keys:
        value = metadata.get(key)
        if value not in (None, ""):
            return value
    return ""


def _message_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "date": _first_text(
            metadata, "sentDate", "sendingDate", "depositDate", "communicationDate"
        ),
        "sender": _first_text(
            metadata, "sender", "senderName", "senderDisplayName", "expeditor"
        ),
        "subject": _first_text(
            metadata, "subject", "communicationSubject", "communicationLabel", "label"
        ),
        "raw": metadata,
    }


def _communal_bill_subject(item: dict[str, Any]) -> str:
    parts = [
        str(item.get("administrationName") or "").strip(),
        str(item.get("documentType") or "").strip(),
        str(item.get("reference") or "").strip(),
    ]
    text = " - ".join(part for part in parts if part)
    return text or str(item.get("id") or "Communal bill")


def _communal_bill_metadata(item: dict[str, Any], backend: str) -> dict[str, Any]:
    return {
        "kind": COMMUNAL_BILL_MESSAGE_KIND,
        "date": item.get("creationDate") or "",
        "sender": item.get("administrationName") or backend,
        "subject": _communal_bill_subject(item),
        "backend": backend,
        "reference": item.get("reference"),
        "documentType": item.get("documentType"),
        "raw": item,
    }


def _communal_bill_document_name(item: dict[str, Any]) -> str:
    date_value = str(item.get("creationDate") or "")[:10]
    parts = [
        date_value,
        str(item.get("administrationName") or "").strip(),
        str(item.get("documentType") or "").strip(),
        str(item.get("reference") or "").strip(),
    ]
    stem = " - ".join(part for part in parts if part)
    return f"{stem or str(item.get('id') or 'communal-bill')}.pdf"


def _source_error(error: MyGuichetError) -> SourceError:
    if isinstance(error, SessionExpired):
        return SourceSessionExpired(str(error))
    if isinstance(error, PortalResponseError):
        return SourceResponseError(str(error))
    return SourceError(str(error))


def collect_unseen_communications(
    client: MyGuichetClient, seen: set[str]
) -> list[tuple[str, dict[str, Any]]]:
    """Page through the inbox until reaching an already-seen section."""
    unseen: list[tuple[str, dict[str, Any]]] = []
    collected_ids: set[str] = set()
    page_number = 1
    retrieved_count = 0
    while True:
        payload = client.list_communications(
            page=page_number, per_page=REQUESTS_PER_PAGE
        )
        raw_items = payload.get("myCommunicationList", [])
        if not isinstance(raw_items, list):
            raise PortalResponseError(
                "Portal response does not contain a communication list."
            )
        if not raw_items:
            break
        retrieved_count += len(raw_items)

        page_ids: list[str] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            hit = item.get("eDeliveryCommunicationHitDto") or {}
            if not isinstance(hit, dict) or hit.get("id") is None:
                continue
            communication_id = str(hit["id"])
            page_ids.append(communication_id)
            if communication_id not in seen and communication_id not in collected_ids:
                unseen.append((communication_id, hit))
                collected_ids.add(communication_id)

        total = payload.get("nbTotalCommunication")
        at_end = isinstance(total, int) and retrieved_count >= total
        if at_end or (
            not isinstance(total, int) and len(raw_items) < REQUESTS_PER_PAGE
        ):
            break
        if page_ids and all(communication_id in seen for communication_id in page_ids):
            break
        page_number += 1

    unseen.reverse()
    return unseen


def communal_bill_backends(client: MyGuichetClient) -> list[str]:
    """Return accepted communal-bill backends, falling back to the captured VDL flow."""
    payload = client.list_communal_bill_consent_status()
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise PortalResponseError("Portal response does not contain consent status data.")

    backends: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        origin = row.get("origin")
        if (
            isinstance(origin, str)
            and origin
            and row.get("consentStatus") == "ACCEPTED"
            and origin not in seen
        ):
            backends.append(origin)
            seen.add(origin)
    return backends or [DEFAULT_COMMUNAL_BILL_BACKEND]


def collect_communal_bills(
    client: MyGuichetClient,
    seen: set[str],
    *,
    page_size: int = COMMUNAL_BILLS_PER_PAGE,
) -> list[tuple[str, dict[str, Any], str]]:
    """Page through communal bills for all accepted compatible backends."""
    unseen: list[tuple[str, dict[str, Any], str]] = []
    collected_ids: set[str] = set()
    for backend in communal_bill_backends(client):
        page_number = 1
        while True:
            payload = client.list_communal_bills(
                backend=backend,
                page_number=page_number,
                page_size=page_size,
            )
            raw_items = payload.get("items")
            if not isinstance(raw_items, list):
                raise PortalResponseError(
                    "Portal response does not contain a communal-bill item list."
                )
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                bill_id = item.get("id")
                if (
                    isinstance(bill_id, str)
                    and bill_id
                    and bill_id not in seen
                    and bill_id not in collected_ids
                ):
                    unseen.append((bill_id, item, backend))
                    collected_ids.add(bill_id)

            page_count = payload.get("pageCount")
            total_count = payload.get("totalCount")
            if isinstance(page_count, int):
                if page_number >= page_count:
                    break
            elif isinstance(total_count, int):
                if page_number * page_size >= total_count:
                    break
            elif len(raw_items) < page_size:
                break
            if not raw_items:
                break
            page_number += 1

    unseen.reverse()
    return unseen


class MyGuichetDocumentSource:
    """Source adapter that plugs the MyGuichet client into the core."""

    name = "myguichet"

    def __init__(self) -> None:
        self.client: MyGuichetClient | None = None
        self.myguichet_account: MyGuichetAccountConfig | None = None

    def _account(self, account: SourceAccountConfig) -> MyGuichetAccountConfig:
        if self.myguichet_account is None:
            self.myguichet_account = myguichet_account_from_source(account)
        return self.myguichet_account

    def _load_cookie(self, account: MyGuichetAccountConfig) -> str:
        if account.cookie_file.exists():
            restrict_file(account.cookie_file)
            cookie = account.cookie_file.read_text(encoding="utf-8").strip()
            if cookie:
                return cookie
        print(f"[{account.name}] No usable session cookie found; starting LuxTrust login.")
        return self._refresh_cookie(account)

    def _client(self, account: SourceAccountConfig) -> MyGuichetClient:
        myguichet_account = self._account(account)
        if self.client is None:
            self.client = MyGuichetClient(
                self._load_cookie(myguichet_account),
                myguichet_account.space_id,
                myguichet_account.language,
            )
        return self.client

    def _refresh_cookie(self, account: MyGuichetAccountConfig) -> str:
        from sources.myguichet.login import LoginError, refresh_cookie

        try:
            return refresh_cookie(account)
        except LoginError as error:
            raise SourceError(
                f"Could not refresh the MyGuichet session for {account.name}: {error}"
            ) from error

    def collect_unseen(
        self, account: SourceAccountConfig, context: SourceContext, seen: set[str]
    ) -> list[SourceMessage]:
        del context
        try:
            client = self._client(account)
            communications = collect_unseen_communications(client, seen)
            communication_messages = [
                SourceMessage(id=communication_id, metadata=_message_metadata(metadata))
                for communication_id, metadata in communications
            ]
        except MyGuichetError as error:
            raise _source_error(error) from error

        collected_ids = {message.id for message in communication_messages}
        try:
            communal_bills = collect_communal_bills(client, seen | collected_ids)
        except SessionExpired as error:
            raise _source_error(error) from error
        except MyGuichetError as error:
            print(f"[{account.name}] Could not list communal bills: {error}")
            communal_bills = []
        bill_messages = [
            SourceMessage(id=bill_id, metadata=_communal_bill_metadata(item, backend))
            for bill_id, item, backend in communal_bills
        ]
        return communication_messages + bill_messages

    def list_documents(
        self,
        account: SourceAccountConfig,
        context: SourceContext,
        message: SourceMessage,
    ) -> list[SourceDocument]:
        del context
        if message.metadata.get("kind") == COMMUNAL_BILL_MESSAGE_KIND:
            item = message.metadata.get("raw")
            if not isinstance(item, dict):
                raise SourceResponseError(
                    f"Communal bill {message.id} has invalid metadata."
                )
            return [
                SourceDocument(
                    id=message.id,
                    name=_communal_bill_document_name(item),
                    content_type=str(item.get("mimeType") or "application/pdf"),
                    metadata={"bill": item, "backend": message.metadata.get("backend")},
                )
            ]

        try:
            detail = self._client(account).get_edelivery(message.id)
        except MyGuichetError as error:
            raise _source_error(error) from error

        attachments = detail.get("attachmentList", [])
        if not isinstance(attachments, list):
            raise SourceResponseError(
                f"Message {message.id} has an invalid attachment list."
            )

        documents: list[SourceDocument] = []
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            document_id = attachment.get("externalDocId")
            if not document_id:
                continue
            original_name = str(attachment.get("docName") or document_id)
            documents.append(
                SourceDocument(
                    id=str(document_id),
                    name=original_name,
                    metadata={"detail": detail, "attachment": attachment},
                )
            )
        return documents

    def open_document(
        self,
        account: SourceAccountConfig,
        context: SourceContext,
        message: SourceMessage,
        document: SourceDocument,
    ) -> DownloadResponse:
        del context
        try:
            if message.metadata.get("kind") == COMMUNAL_BILL_MESSAGE_KIND:
                backend = document.metadata.get("backend")
                if not isinstance(backend, str) or not backend:
                    raise SourceResponseError(
                        f"Communal bill {message.id} has no backend metadata."
                    )
                return self._client(account).download_communal_bill(document.id, backend)
            return self._client(account).download_document(document.id, document.name)
        except MyGuichetError as error:
            raise _source_error(error) from error

    def refresh_authentication(
        self, account: SourceAccountConfig, context: SourceContext
    ) -> None:
        del context
        self.close()
        self._refresh_cookie(self._account(account))

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None
