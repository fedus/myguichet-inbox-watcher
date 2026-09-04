"""MyGuichet document source adapter."""

from __future__ import annotations

from typing import Any

from sources.base import (
    DownloadResponse,
    SourceAccountConfig,
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
        self, account: SourceAccountConfig, seen: set[str]
    ) -> list[SourceMessage]:
        try:
            communications = collect_unseen_communications(self._client(account), seen)
        except MyGuichetError as error:
            raise _source_error(error) from error
        return [
            SourceMessage(id=communication_id, metadata=_message_metadata(metadata))
            for communication_id, metadata in communications
        ]

    def list_documents(
        self, account: SourceAccountConfig, message: SourceMessage
    ) -> list[SourceDocument]:
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
        message: SourceMessage,
        document: SourceDocument,
    ) -> DownloadResponse:
        del message
        try:
            return self._client(account).download_document(document.id, document.name)
        except MyGuichetError as error:
            raise _source_error(error) from error

    def refresh_authentication(self, account: SourceAccountConfig) -> None:
        self.close()
        self._refresh_cookie(self._account(account))

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None
