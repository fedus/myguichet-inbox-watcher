"""ProSyndic document source adapter."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
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
from sources.prosyndic.client import (
    ProSyndicAuthenticationError,
    ProSyndicClient,
    ProSyndicError,
    ProSyndicResponseError,
    ProSyndicSessionExpired,
)
from sources.prosyndic.config import (
    DEFAULT_PAGE_LIMIT,
    ProSyndicAccountConfig,
    prosyndic_account_from_source,
)


@dataclass(frozen=True)
class ProSyndicRemoteDocument:
    """One ProSyndic document plus its folder location."""

    id: str
    name: str
    content_type: str
    folder_id: str | None
    folder_path: tuple[str, ...]
    metadata: dict[str, Any]


def _source_error(error: ProSyndicError) -> SourceError:
    if isinstance(error, ProSyndicSessionExpired):
        return SourceSessionExpired(str(error))
    if isinstance(error, (ProSyndicAuthenticationError, ProSyndicResponseError)):
        return SourceResponseError(str(error))
    return SourceError(str(error))


def _payload_data(payload: dict[str, Any], description: str) -> dict[str, Any]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ProSyndicResponseError(f"ProSyndic {description} is missing data.")
    return data


def _payload_pages(payload: dict[str, Any]) -> dict[str, Any]:
    pages = payload.get("pages")
    return pages if isinstance(pages, dict) else {}


def _last_page(payload: dict[str, Any]) -> int:
    pages = _payload_pages(payload)
    last = pages.get("last") or pages.get("pageCount") or pages.get("current") or 1
    if isinstance(last, int) and last > 0:
        return last
    return 1


def _document_name(document: dict[str, Any]) -> str:
    for key in ("title", "name", "file_name"):
        value = document.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(document["id"])


def document_from_payload(
    document: dict[str, Any],
    folder_id: str | None,
    folder_path: tuple[str, ...],
) -> ProSyndicRemoteDocument | None:
    """Map one folder-listing document item into the source model."""
    document_id = document.get("id")
    if not isinstance(document_id, str) or not document_id.strip():
        return None
    content_type = document.get("mime_type")
    if not isinstance(content_type, str) or not content_type.strip():
        content_type = "application/pdf"
    return ProSyndicRemoteDocument(
        id=document_id.strip(),
        name=_document_name(document),
        content_type=content_type.strip(),
        folder_id=folder_id,
        folder_path=folder_path,
        metadata=dict(document),
    )


def _folder_name(folder: dict[str, Any]) -> str:
    value = folder.get("nom") or folder.get("name") or folder.get("id")
    return str(value).strip() if value is not None else ""


def collect_documents(
    client: ProSyndicClient,
    *,
    page_limit: int = DEFAULT_PAGE_LIMIT,
) -> list[ProSyndicRemoteDocument]:
    """Traverse ProSyndic folders and return all listed documents."""
    documents: list[ProSyndicRemoteDocument] = []
    queued: list[tuple[str | None, tuple[str, ...]]] = [(None, ())]
    visited_folders: set[str | None] = set()
    seen_documents: set[str] = set()

    while queued:
        folder_id, folder_path = queued.pop(0)
        if folder_id in visited_folders:
            continue
        visited_folders.add(folder_id)

        page = 1
        while True:
            payload = client.list_folder(folder_id, page=page, limit=page_limit)
            data = _payload_data(payload, "folder listing")
            raw_folders = data.get("Classeurs")
            if not isinstance(raw_folders, list):
                raise ProSyndicResponseError(
                    "ProSyndic folder listing is missing folders."
                )
            raw_documents = data.get("Documents")
            if not isinstance(raw_documents, list):
                raise ProSyndicResponseError(
                    "ProSyndic folder listing is missing documents."
                )

            for raw_folder in raw_folders:
                if not isinstance(raw_folder, dict):
                    continue
                raw_id = raw_folder.get("id")
                if not isinstance(raw_id, str) or not raw_id.strip():
                    continue
                child_id = raw_id.strip()
                child_name = _folder_name(raw_folder)
                child_path = folder_path + ((child_name,) if child_name else ())
                queued.append((child_id, child_path))

            for raw_document in raw_documents:
                if not isinstance(raw_document, dict):
                    continue
                document = document_from_payload(raw_document, folder_id, folder_path)
                if document is None or document.id in seen_documents:
                    continue
                documents.append(document)
                seen_documents.add(document.id)

            if page >= _last_page(payload):
                break
            page += 1

    documents.sort(
        key=lambda document: (
            str(document.metadata.get("date_commit") or ""),
            document.id,
        )
    )
    return documents


def message_from_document(document: ProSyndicRemoteDocument) -> SourceMessage:
    """Represent one remote ProSyndic document as one source message."""
    return SourceMessage(
        id=document.id,
        metadata={
            "subject": document.name,
            "date": document.metadata.get("date_commit"),
            "folder": " / ".join(document.folder_path),
            "raw": document.metadata,
        },
    )


def source_document_from_remote(document: ProSyndicRemoteDocument) -> SourceDocument:
    """Convert one remote ProSyndic document to a downloadable source document."""
    return SourceDocument(
        id=document.id,
        name=document.name,
        content_type=document.content_type,
        metadata={
            "title": document.name,
            "date": document.metadata.get("date_commit"),
            "folder": " / ".join(document.folder_path),
            "raw": document.metadata,
        },
    )


class ProSyndicDocumentSource:
    """Source adapter for ProSyndic extranet documents."""

    name = "prosyndic"

    def __init__(
        self,
        client_factory: Callable[[str], ProSyndicClient] = ProSyndicClient,
    ) -> None:
        self.client_factory = client_factory
        self.client: ProSyndicClient | None = None
        self.prosyndic_account: ProSyndicAccountConfig | None = None
        self.documents_by_id: dict[str, ProSyndicRemoteDocument] = {}
        self.authenticated = False

    def _account(self, account: SourceAccountConfig) -> ProSyndicAccountConfig:
        if self.prosyndic_account is None:
            self.prosyndic_account = prosyndic_account_from_source(account)
        return self.prosyndic_account

    def _client(self, account: ProSyndicAccountConfig) -> ProSyndicClient:
        if self.client is None:
            self.client = self.client_factory(account.base_url)
        return self.client

    def _authenticated_client(
        self, account: SourceAccountConfig, context: SourceContext
    ) -> ProSyndicClient:
        del context
        prosyndic_account = self._account(account)
        client = self._client(prosyndic_account)
        if not self.authenticated:
            client.login(prosyndic_account.username, prosyndic_account.password)
            self.authenticated = True
        return client

    def collect_unseen(
        self, account: SourceAccountConfig, context: SourceContext, seen: set[str]
    ) -> list[SourceMessage]:
        prosyndic_account = self._account(account)
        try:
            documents = collect_documents(
                self._authenticated_client(account, context),
                page_limit=prosyndic_account.page_limit,
            )
        except ProSyndicError as error:
            raise _source_error(error) from error
        self.documents_by_id = {document.id: document for document in documents}
        return [
            message_from_document(document)
            for document in documents
            if document.id not in seen
        ]

    def list_documents(
        self,
        account: SourceAccountConfig,
        context: SourceContext,
        message: SourceMessage,
    ) -> list[SourceDocument]:
        del account, context
        try:
            document = self.documents_by_id[message.id]
        except KeyError:
            raise SourceResponseError(
                f"ProSyndic document {message.id} was not present in the last listing."
            ) from None
        return [source_document_from_remote(document)]

    def open_document(
        self,
        account: SourceAccountConfig,
        context: SourceContext,
        message: SourceMessage,
        document: SourceDocument,
    ) -> DownloadResponse:
        del message
        try:
            return self._authenticated_client(account, context).download_document(
                document.id
            )
        except ProSyndicError as error:
            raise _source_error(error) from error

    def refresh_authentication(
        self, account: SourceAccountConfig, context: SourceContext
    ) -> None:
        prosyndic_account = self._account(account)
        self.authenticated = False
        self._client(prosyndic_account).login(
            prosyndic_account.username,
            prosyndic_account.password,
        )
        self.authenticated = True

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None
