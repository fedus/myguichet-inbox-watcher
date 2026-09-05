"""DKV/Lalux EasyApp source adapter for treated reimbursements."""

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


def collect_treated_refunds(
    client: DkvClient, seen: set[str], page_limit: int = DEFAULT_PAGE_LIMIT
) -> list[tuple[str, dict[str, Any]]]:
    """Read every refund list page and return unseen treated reimbursements."""
    unseen: list[tuple[str, dict[str, Any]]] = []
    collected_ids: set[str] = set()
    page_index = 0
    while True:
        payload = client.list_refunds(page_index=page_index, limit=page_limit)
        items = _items_from_page(payload)
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

        paging = _paging_info(payload)
        if paging is None:
            if len(items) < page_limit:
                break
        else:
            limit, offset, total = paging
            if offset + len(items) >= total:
                break
            if limit > 0:
                page_limit = limit
        if not items:
            break
        page_index += 1

    unseen.reverse()
    return unseen


def _message_metadata(refund: dict[str, Any]) -> dict[str, Any]:
    return {
        "subject": refund.get("title") or refund.get("description") or "Reimbursement",
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
    """Source adapter for DKV/Lalux EasyApp treated reimbursements."""

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
            refunds = collect_treated_refunds(
                self._authenticated_client(account, context),
                seen,
                dkv_account.page_limit,
            )
        except DkvError as error:
            raise _source_error(error) from error
        return [
            SourceMessage(id=refund_id, metadata=_message_metadata(metadata))
            for refund_id, metadata in refunds
        ]

    def list_documents(
        self,
        account: SourceAccountConfig,
        context: SourceContext,
        message: SourceMessage,
    ) -> list[SourceDocument]:
        try:
            detail = self._authenticated_client(account, context).get_refund(message.id)
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
