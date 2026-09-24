"""DKV/Lalux EasyApp source adapter."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from input_broker import InputChallenge
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
from sources.dkv.client import (
    DkvAuthenticationError,
    DkvClient,
    DkvError,
    DkvResponseError,
    DkvSessionExpired,
)
from sources.dkv.config import (
    DEFAULT_PAGE_LIMIT,
    DkvAccountConfig,
    dkv_account_from_source,
)
from storage import atomic_write_text, restrict_file


TREATED_STATUS_CODE = "TREATED"
TOKEN_EXPIRY_SKEW_SECONDS = 30
MESSAGE_KIND_AVAILABLE_DOCUMENT = "available_document"
MESSAGE_KIND_INVOICE = "invoice"
MESSAGE_KIND_REFUND = "refund"


def _source_error(error: DkvError) -> SourceError:
    if isinstance(error, DkvSessionExpired):
        return SourceSessionExpired(str(error))
    if isinstance(error, (DkvAuthenticationError, DkvResponseError)):
        return SourceResponseError(str(error))
    return SourceError(str(error))


def _items_from_page(payload: dict[str, Any]) -> list[dict[str, Any]]:
    groups = payload.get("groups")
    if not isinstance(groups, list):
        raise DkvResponseError("DKV/Lalux EasyApp refund list is missing groups.")
    items: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        raw_items = group.get("items")
        if not isinstance(raw_items, list):
            continue
        items.extend(item for item in raw_items if isinstance(item, dict))
    return items


def _paging_info(payload: dict[str, Any]) -> tuple[int, int, int] | None:
    paging = payload.get("pagingInfo")
    if not isinstance(paging, dict):
        return None
    limit = paging.get("limit")
    offset = paging.get("offset")
    total = paging.get("total")
    if all(isinstance(value, int) for value in (limit, offset, total)):
        return limit, offset, total
    return None


def _paginated_items(
    fetch_page: Callable[[int, int], dict[str, Any]], page_limit: int
) -> list[dict[str, Any]]:
    """Read every infinite-scroll page for endpoints with groups/pagingInfo."""
    items: list[dict[str, Any]] = []
    page_index = 0
    while True:
        payload = fetch_page(page_index, page_limit)
        page_items = _items_from_page(payload)
        items.extend(page_items)

        paging = _paging_info(payload)
        if paging is None:
            if len(page_items) < page_limit:
                break
        else:
            limit, offset, total = paging
            if offset + len(page_items) >= total:
                break
            if limit > 0:
                page_limit = limit
        if not page_items:
            break
        page_index += 1
    return items


def collect_treated_refunds(
    client: DkvClient, seen: set[str], page_limit: int = DEFAULT_PAGE_LIMIT
) -> list[tuple[str, dict[str, Any]]]:
    """Read every refund list page and return unseen treated reimbursements."""
    unseen: list[tuple[str, dict[str, Any]]] = []
    collected_ids: set[str] = set()
    items = _paginated_items(client.list_refunds, page_limit)
    for item in items:
        refund_id = item.get("id")
        if (
            isinstance(refund_id, str)
            and item.get("statusCode") == TREATED_STATUS_CODE
            and refund_id not in seen
            and refund_id not in collected_ids
        ):
            unseen.append((refund_id, item))
            collected_ids.add(refund_id)

    unseen.reverse()
    return unseen


def _message_metadata(refund: dict[str, Any]) -> dict[str, Any]:
    return {
        "subject": refund.get("title") or refund.get("description") or "Reimbursement",
        "kind": MESSAGE_KIND_REFUND,
        "status": refund.get("status"),
        "statusCode": refund.get("statusCode"),
        "raw": refund,
    }


def documents_from_refund_detail(
    refund_id: str, detail: dict[str, Any]
) -> list[SourceDocument]:
    """Return downloadable documents for one treated reimbursement detail."""
    if detail.get("statusCode") != TREATED_STATUS_CODE:
        raise SourceResponseError(
            f"Reimbursement {refund_id} is {detail.get('statusCode')!r}, not TREATED."
        )
    documents = detail.get("listDocument")
    if not isinstance(documents, list):
        raise SourceResponseError(
            f"Reimbursement {refund_id} has an invalid document list."
        )

    result: list[SourceDocument] = []
    for document in documents:
        if not isinstance(document, dict):
            continue
        document_id = document.get("idDocument")
        if not isinstance(document_id, str) or not document_id:
            continue
        name = str(document.get("label") or document_id)
        result.append(
            SourceDocument(
                id=document_id,
                name=name,
                content_type="application/pdf",
                metadata={"refund": detail, "document": document},
            )
        )
    return result


def _available_document_metadata(
    document: dict[str, Any],
    category: dict[str, Any],
    subcategory: dict[str, Any],
    source_type: str,
) -> dict[str, Any]:
    return {
        "subject": document.get("label") or document.get("idDocument") or "Document",
        "kind": MESSAGE_KIND_AVAILABLE_DOCUMENT,
        "source_type": source_type,
        "category": category.get("libelleCategory") or category.get("idCategory"),
        "subcategory": subcategory.get("title"),
        "raw": {
            "document": document,
            "category": category,
            "subcategory": subcategory,
        },
    }


def collect_available_documents(
    categories: list[Any],
    seen: set[str],
    *,
    source_type: str,
) -> list[SourceMessage]:
    """Flatten the DKV document-tab category tree into unseen messages."""
    messages: list[SourceMessage] = []
    collected_ids: set[str] = set()
    for category in categories:
        if not isinstance(category, dict):
            continue
        subcategories = category.get("subCategories")
        if not isinstance(subcategories, list):
            continue
        for subcategory in subcategories:
            if not isinstance(subcategory, dict):
                continue
            documents = subcategory.get("documents")
            if not isinstance(documents, list):
                continue
            for document in documents:
                if not isinstance(document, dict):
                    continue
                document_id = document.get("idDocument")
                if (
                    not isinstance(document_id, str)
                    or not document_id
                    or document_id in seen
                    or document_id in collected_ids
                ):
                    continue
                messages.append(
                    SourceMessage(
                        id=document_id,
                        metadata=_available_document_metadata(
                            document, category, subcategory, source_type
                        ),
                    )
                )
                collected_ids.add(document_id)
    return messages


def document_from_available_message(message: SourceMessage) -> SourceDocument:
    raw = message.metadata.get("raw")
    document = raw.get("document") if isinstance(raw, dict) else None
    if not isinstance(document, dict):
        raise SourceResponseError(
            f"DKV/Lalux EasyApp document {message.id!r} is missing metadata."
        )
    name = str(document.get("label") or message.id)
    return SourceDocument(
        id=message.id,
        name=name,
        content_type="application/pdf",
        metadata=message.metadata,
    )


def collect_invoice_messages(
    client: DkvClient, seen: set[str], page_limit: int
) -> list[SourceMessage]:
    """Read invoice pages and return unseen invoices that expose a document."""
    items = _paginated_items(client.list_invoices, page_limit)
    messages: list[SourceMessage] = []
    collected_ids: set[str] = set()
    for item in items:
        invoice_id = item.get("id")
        if (
            not isinstance(invoice_id, str)
            or not invoice_id
            or invoice_id in seen
            or invoice_id in collected_ids
            or item.get("documentAvailable") is not True
        ):
            continue
        messages.append(
            SourceMessage(
                id=invoice_id,
                metadata={
                    "subject": item.get("label") or item.get("amount") or "Invoice",
                    "kind": MESSAGE_KIND_INVOICE,
                    "date": item.get("date"),
                    "status": item.get("status"),
                    "statusCode": item.get("statusCode"),
                    "raw": item,
                },
            )
        )
        collected_ids.add(invoice_id)
    messages.reverse()
    return messages


def document_from_invoice_detail(invoice_id: str, detail: dict[str, Any]) -> SourceDocument:
    document_id = detail.get("gedDocumentId")
    if not isinstance(document_id, str) or not document_id:
        raise SourceResponseError(f"Invoice {invoice_id} does not expose a document ID.")
    return SourceDocument(
        id=document_id,
        name=str(detail.get("label") or invoice_id),
        content_type="application/pdf",
        metadata={"invoice": detail},
    )


def _token_with_expiry(payload: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    token = dict(payload)
    expires_in = payload.get("expires_in")
    refresh_expires_in = payload.get("refresh_expires_in")
    if isinstance(expires_in, int):
        token["expires_at"] = now + expires_in
    if isinstance(refresh_expires_in, int):
        token["refresh_expires_at"] = now + refresh_expires_in
    return token


def _token_valid(token: dict[str, Any], key: str) -> bool:
    expires_at = token.get(key)
    return isinstance(expires_at, (int, float)) and (
        time.time() + TOKEN_EXPIRY_SKEW_SECONDS < expires_at
    )


class DkvDocumentSource:
    """Source adapter for DKV/Lalux EasyApp documents."""

    name = "dkv"

    def __init__(
        self, client_factory: Callable[[], DkvClient] = DkvClient
    ) -> None:
        self.client_factory = client_factory
        self.client: DkvClient | None = None
        self.dkv_account: DkvAccountConfig | None = None
        self.token: dict[str, Any] | None = None

    def _account(self, account: SourceAccountConfig) -> DkvAccountConfig:
        if self.dkv_account is None:
            self.dkv_account = dkv_account_from_source(account)
        return self.dkv_account

    def _client(self) -> DkvClient:
        if self.client is None:
            self.client = self.client_factory()
        return self.client

    def _load_token(self, account: DkvAccountConfig) -> dict[str, Any] | None:
        if self.token is not None:
            return self.token
        if not account.token_file.exists():
            return None
        restrict_file(account.token_file)
        try:
            payload = json.loads(account.token_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SourceResponseError(
                f"Could not read {account.token_file}; remove it to log in again."
            ) from error
        if not isinstance(payload, dict):
            raise SourceResponseError(f"{account.token_file} has an invalid format.")
        self.token = payload
        return payload

    def _save_token(self, account: DkvAccountConfig, payload: dict[str, Any]) -> None:
        self.token = _token_with_expiry(payload)
        atomic_write_text(
            account.token_file,
            json.dumps(self.token, indent=2, sort_keys=True) + "\n",
        )

    def _authenticate_with_otp(
        self, account: DkvAccountConfig, context: SourceContext
    ) -> None:
        client = self._client()
        started = client.start_sms_login(
            account.username, account.password, account.otp_type
        )
        if isinstance(started, dict):
            self._save_token(account, started)
            client.set_access_token(str(started["access_token"]))
            return

        answer = context.request_input(
            InputChallenge(
                account_name=account.name,
                source="dkv",
                kind="otp",
                prompt="Enter the DKV/Lalux EasyApp SMS one-time code",
                timeout_seconds=account.otp_timeout_seconds,
            )
        )
        otp = answer.get("code", "").strip()
        if not otp:
            raise SourceError("DKV/Lalux EasyApp OTP code was empty.")
        token = client.complete_sms_login(started, otp, account.otp_type)
        self._save_token(account, token)
        client.set_access_token(str(token["access_token"]))

    def _ensure_authenticated(
        self, account: DkvAccountConfig, context: SourceContext
    ) -> DkvClient:
        client = self._client()
        token = self._load_token(account)
        if token and _token_valid(token, "expires_at"):
            client.set_access_token(str(token["access_token"]))
            return client

        if token and token.get("refresh_token") and _token_valid(
            token, "refresh_expires_at"
        ):
            try:
                refreshed = client.refresh_access_token(str(token["refresh_token"]))
            except DkvSessionExpired:
                pass
            else:
                self._save_token(account, refreshed)
                client.set_access_token(str(refreshed["access_token"]))
                return client

        self._authenticate_with_otp(account, context)
        return client

    def _authenticated_client(
        self, account: SourceAccountConfig, context: SourceContext
    ) -> DkvClient:
        return self._ensure_authenticated(self._account(account), context)

    def collect_unseen(
        self, account: SourceAccountConfig, context: SourceContext, seen: set[str]
    ) -> list[SourceMessage]:
        dkv_account = self._account(account)
        try:
            client = self._authenticated_client(account, context)
            refunds = collect_treated_refunds(
                client, seen, dkv_account.page_limit
            )
            refund_messages = [
                SourceMessage(id=refund_id, metadata=_message_metadata(metadata))
                for refund_id, metadata in refunds
            ]
            collected_ids = {message.id for message in refund_messages}
            available_documents = collect_available_documents(
                client.list_available_documents(),
                seen | collected_ids,
                source_type="available",
            )
            collected_ids.update(message.id for message in available_documents)
            on_demand_documents = collect_available_documents(
                client.list_on_demand_documents(),
                seen | collected_ids,
                source_type="on-demand",
            )
            collected_ids.update(message.id for message in on_demand_documents)
            invoice_messages = collect_invoice_messages(
                client, seen | collected_ids, dkv_account.page_limit
            )
        except DkvError as error:
            raise _source_error(error) from error
        return (
            refund_messages
            + available_documents
            + on_demand_documents
            + invoice_messages
        )

    def list_documents(
        self,
        account: SourceAccountConfig,
        context: SourceContext,
        message: SourceMessage,
    ) -> list[SourceDocument]:
        try:
            client = self._authenticated_client(account, context)
            kind = message.metadata.get("kind")
            if kind == MESSAGE_KIND_AVAILABLE_DOCUMENT:
                return [document_from_available_message(message)]
            if kind == MESSAGE_KIND_INVOICE:
                detail = client.get_invoice(message.id)
                return [document_from_invoice_detail(message.id, detail)]
            detail = client.get_refund(message.id)
            return documents_from_refund_detail(message.id, detail)
        except DkvError as error:
            raise _source_error(error) from error

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
        except DkvError as error:
            raise _source_error(error) from error

    def refresh_authentication(
        self, account: SourceAccountConfig, context: SourceContext
    ) -> None:
        dkv_account = self._account(account)
        self.token = None
        if dkv_account.token_file.exists():
            dkv_account.token_file.unlink()
        self._authenticate_with_otp(dkv_account, context)

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None
