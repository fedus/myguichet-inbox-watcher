"""Foyer document source adapter."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
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
from sources.foyer.client import (
    FoyerAuthenticationError,
    FoyerClient,
    FoyerError,
    FoyerResponseError,
    FoyerSessionExpired,
)
from sources.foyer.config import FoyerAccountConfig, foyer_account_from_source
from storage import atomic_write_text, restrict_file


TOKEN_EXPIRY_SKEW_SECONDS = 30
LIFE_DOCUMENT_CODES = (
    "SITCPTPF",
    "SITCPTAN",
    "PROFIL_INVESTISSEUR",
    "CNFRMARB",
    "CNFRMRAC",
    "CNFRMVRS",
    "PV_DE_CONSEIL",
    "CERTIMPOTS",
    "CONDIPARTI_SIGNEE",
    "AFFAIRE_NOUVELLE",
)
BILAN_DOCUMENT_CODES = ("BILAN360PRO", "BILAN360PAR")


@dataclass(frozen=True)
class FoyerRemoteDocument:
    """One downloadable Foyer document from any customer document category."""

    id: str
    source_kind: str
    name: str
    content_type: str
    download_url: str
    date_value: str
    metadata: dict[str, Any]


def _source_error(error: FoyerError) -> SourceError:
    if isinstance(error, FoyerSessionExpired):
        return SourceSessionExpired(str(error))
    if isinstance(error, (FoyerAuthenticationError, FoyerResponseError)):
        return SourceResponseError(str(error))
    return SourceError(str(error))


def _token_with_expiry(payload: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    token = dict(payload)
    expires_in = payload.get("expires_in")
    if isinstance(expires_in, int):
        token["expires_at"] = now + expires_in
    return token


def _token_valid(token: dict[str, Any]) -> bool:
    expires_at = token.get("expires_at")
    return isinstance(expires_at, (int, float)) and (
        time.time() + TOKEN_EXPIRY_SKEW_SECONDS < expires_at
    )


def _lookback_date(years: int) -> date:
    today = datetime.now(timezone.utc).date()
    try:
        return today.replace(year=today.year - years)
    except ValueError:
        return today.replace(year=today.year - years, day=28)


def _page_items(payload: dict[str, Any], description: str) -> list[dict[str, Any]]:
    data = payload.get("data")
    if not isinstance(data, list):
        raise FoyerResponseError(f"Foyer {description} listing is missing data.")
    return [item for item in data if isinstance(item, dict)]


def _offset_page_finished(payload: dict[str, Any], item_count: int, offset: int) -> bool:
    meta = payload.get("meta")
    if isinstance(meta, dict):
        total = meta.get("totalRecords")
        page = meta.get("page")
        if isinstance(page, dict):
            page_offset = page.get("offset")
            if isinstance(page_offset, int):
                offset = page_offset
        if isinstance(total, int):
            return offset + item_count >= total
    return item_count == 0


def _number_page_finished(payload: dict[str, Any], number: int) -> bool:
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        return True
    total_pages = meta.get("totalPages")
    if isinstance(total_pages, int):
        return number + 1 >= total_pages
    return True


def _attributes(item: dict[str, Any]) -> dict[str, Any]:
    attrs = item.get("attributes")
    return attrs if isinstance(attrs, dict) else {}


def _label_from_code(value: object) -> str:
    if isinstance(value, dict):
        label = value.get("label") or value.get("code")
        if label is not None:
            return str(label)
    if value is not None:
        return str(value)
    return ""


def _document_name(source_kind: str, item_id: str, attrs: dict[str, Any]) -> str:
    candidates = (
        _label_from_code(attrs.get("codeDocument")),
        _label_from_code(attrs.get("document")),
        str(attrs.get("numeroFacture") or ""),
        str(attrs.get("codeDoc") or ""),
    )
    for value in candidates:
        text = value.strip()
        if text:
            return text
    return f"{source_kind}-{item_id}"


def _document_date(attrs: dict[str, Any]) -> str:
    for key in ("dateEmission", "dateCreation", "date", "uploadedAt"):
        value = attrs.get(key)
        if value:
            return str(value)
    return ""


def remote_document_from_item(
    source_kind: str, item: dict[str, Any]
) -> FoyerRemoteDocument | None:
    """Convert one JSON:API item into a downloadable remote document."""
    raw_id = item.get("id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        return None
    attrs = _attributes(item)
    download_url = attrs.get("url") or attrs.get("href")
    if not isinstance(download_url, str) or not download_url.strip():
        return None
    item_id = raw_id.strip()
    return FoyerRemoteDocument(
        id=f"{source_kind}:{item_id}",
        source_kind=source_kind,
        name=_document_name(source_kind, item_id, attrs),
        content_type="application/pdf",
        download_url=download_url.strip(),
        date_value=_document_date(attrs),
        metadata={"item": item, "source_kind": source_kind},
    )


def _offset_pages(
    client: FoyerClient,
    path: str,
    *,
    base_params: dict[str, str | int],
    limit: int,
    description: str,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    offset = 0
    while True:
        params = dict(base_params)
        params["page[limit]"] = limit
        params["page[offset]"] = offset
        payload = client.list_json(path, params)
        page_items = _page_items(payload, description)
        for item in page_items:
            item_id = item.get("id")
            if isinstance(item_id, str) and item_id in seen_ids:
                continue
            if isinstance(item_id, str):
                seen_ids.add(item_id)
            items.append(item)
        if _offset_page_finished(payload, len(page_items), offset):
            break
        offset += limit
    return items


def _number_pages(
    client: FoyerClient,
    path: str,
    *,
    base_params: dict[str, str | int],
    limit: int,
    description: str,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    number = 0
    while True:
        params = dict(base_params)
        params["page[size]"] = limit
        params["page[number]"] = number
        payload = client.list_json(path, params)
        page_items = _page_items(payload, description)
        items.extend(page_items)
        if _number_page_finished(payload, number) or not page_items:
            break
        number += 1
    return items


def _client_number(profile: dict[str, Any]) -> str:
    meta = profile.get("meta")
    if isinstance(meta, dict) and meta.get("number") is not None:
        return str(meta["number"])
    subject = profile.get("sub")
    if isinstance(subject, str) and ":" in subject:
        return subject.rsplit(":", 1)[-1]
    raise FoyerResponseError("Foyer user profile did not contain a client number.")


def collect_foyer_documents(
    client: FoyerClient,
    *,
    client_number: str,
    lookback: date,
    page_limit: int,
) -> list[FoyerRemoteDocument]:
    """Collect all Foyer customer documents visible in the document list."""
    lookback_day = lookback.isoformat()
    lookback_datetime = f"{lookback_day}T00:00:00.000Z"
    specs: list[tuple[str, str, str, dict[str, str | int], str]] = [
        (
            "contract",
            "offset",
            "/v1/contrats/documents",
            {
                "sort": "-dateEmission",
                "filter[dateEmission]": f">{lookback_day}",
                "filter[statutDocument]": "ARCHIVED, GENERATED",
                "filter[codeEtatContrat]": "V",
                "filter[codeDocument]": "!CONDIPARTIACTU",
                "filter[role]": "preneur,preneur-non-payeur,assure-sante,personne-groupee,procuration",
                "filter[codeTypeDocument]": "!DEV",
            },
            "contract documents",
        ),
        (
            "invoice",
            "offset",
            "/v1/compta/situation-compte/liste-documents-factures",
            {
                "sort": "-dateCreation",
                "filter[dateCreation]": f">{lookback_day}",
                "filter[client]": client_number,
            },
            "invoice documents",
        ),
        (
            "medical",
            "offset",
            "/prestamed/documents",
            {
                "sort": "-dateEmission",
                "filter[decomptesEtJustificatifs]": "true",
                "filter[dateEmission]": f">{lookback_datetime}",
                "filter[clients]": client_number,
            },
            "medical documents",
        ),
        (
            "life",
            "offset",
            "/v1/vie-individuelle/documents",
            {
                "sort": "-dateCreation",
                "filter[codeDocument]": ",".join(LIFE_DOCUMENT_CODES),
                "filter[dateCreation]": f">{lookback_datetime}",
                "filter[preneur]": client_number,
            },
            "life documents",
        ),
        (
            "bilan",
            "offset",
            "/v1/bilan-client/documents",
            {
                "sort": "-date",
                "filter[codeDoc]": ",".join(BILAN_DOCUMENT_CODES),
                "filter[numeroClient]": client_number,
            },
            "bilan documents",
        ),
        (
            "tavp",
            "number",
            "/v1/tavp/documents",
            {
                "sort": "-uploadedAt",
                "filter[uploadedAt]": f">{lookback_datetime}",
                "filter[clients]": client_number,
            },
            "TAVP documents",
        ),
    ]

    documents: list[FoyerRemoteDocument] = []
    seen_ids: set[str] = set()
    for source_kind, pagination, path, params, description in specs:
        if pagination == "number":
            items = _number_pages(
                client, path, base_params=params, limit=page_limit, description=description
            )
        else:
            items = _offset_pages(
                client, path, base_params=params, limit=page_limit, description=description
            )
        for item in items:
            document = remote_document_from_item(source_kind, item)
            if document is None or document.id in seen_ids:
                continue
            documents.append(document)
            seen_ids.add(document.id)

    documents.sort(key=lambda document: (document.date_value, document.id))
    return documents


def message_from_document(document: FoyerRemoteDocument) -> SourceMessage:
    """Represent one Foyer document as one source message."""
    return SourceMessage(
        id=document.id,
        metadata={
            "subject": document.name,
            "date": document.date_value,
            "source_kind": document.source_kind,
            "raw": document.metadata,
        },
    )


def source_document_from_remote(document: FoyerRemoteDocument) -> SourceDocument:
    """Convert a Foyer remote document to a downloadable source document."""
    return SourceDocument(
        id=document.id,
        name=document.name,
        content_type=document.content_type,
        metadata={
            "title": document.name,
            "date": document.date_value,
            "source_kind": document.source_kind,
            "raw": document.metadata,
        },
    )


class FoyerDocumentSource:
    """Source adapter for Foyer customer documents."""

    name = "foyer"

    def __init__(self, client_factory: Callable[[], FoyerClient] = FoyerClient) -> None:
        self.client_factory = client_factory
        self.client: FoyerClient | None = None
        self.foyer_account: FoyerAccountConfig | None = None
        self.token: dict[str, Any] | None = None
        self.documents_by_id: dict[str, FoyerRemoteDocument] = {}

    def _account(self, account: SourceAccountConfig) -> FoyerAccountConfig:
        if self.foyer_account is None:
            self.foyer_account = foyer_account_from_source(account)
        return self.foyer_account

    def _client(self) -> FoyerClient:
        if self.client is None:
            self.client = self.client_factory()
        return self.client

    def _load_token(self, account: FoyerAccountConfig) -> dict[str, Any] | None:
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

    def _save_token(self, account: FoyerAccountConfig, payload: dict[str, Any]) -> None:
        self.token = _token_with_expiry(payload)
        atomic_write_text(
            account.token_file,
            json.dumps(self.token, indent=2, sort_keys=True) + "\n",
        )

    def _authenticate(self, account: FoyerAccountConfig) -> FoyerClient:
        client = self._client()
        token = client.login(account.username, account.password)
        self._save_token(account, token)
        client.set_access_token(str(token["access_token"]))
        return client

    def _authenticated_client(self, account: SourceAccountConfig) -> FoyerClient:
        foyer_account = self._account(account)
        client = self._client()
        token = self._load_token(foyer_account)
        if token and _token_valid(token):
            client.set_access_token(str(token["access_token"]))
            return client

        refresh_token = token.get("refresh_token") if isinstance(token, dict) else None
        if isinstance(refresh_token, str) and refresh_token:
            try:
                refreshed = client.refresh_access_token(refresh_token)
            except FoyerSessionExpired:
                pass
            else:
                self._save_token(foyer_account, refreshed)
                client.set_access_token(str(refreshed["access_token"]))
                return client

        return self._authenticate(foyer_account)

    def collect_unseen(
        self, account: SourceAccountConfig, context: SourceContext, seen: set[str]
    ) -> list[SourceMessage]:
        del context
        foyer_account = self._account(account)
        try:
            client = self._authenticated_client(account)
            profile = client.user_profile()
            documents = collect_foyer_documents(
                client,
                client_number=_client_number(profile),
                lookback=_lookback_date(foyer_account.lookback_years),
                page_limit=foyer_account.page_limit,
            )
        except FoyerError as error:
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
        document = self.documents_by_id.get(message.id)
        if document is None:
            raise SourceResponseError(
                f"Foyer document {message.id!r} was not present in the current listing."
            )
        return [source_document_from_remote(document)]

    def open_document(
        self,
        account: SourceAccountConfig,
        context: SourceContext,
        message: SourceMessage,
        document: SourceDocument,
    ) -> DownloadResponse:
        del account, context, document
        remote_document = self.documents_by_id.get(message.id)
        if remote_document is None:
            raise SourceResponseError(
                f"Foyer document {message.id!r} was not present in the current listing."
            )
        try:
            return self._client().download_url(remote_document.download_url)
        except FoyerError as error:
            raise _source_error(error) from error

    def refresh_authentication(
        self, account: SourceAccountConfig, context: SourceContext
    ) -> None:
        del context
        foyer_account = self._account(account)
        self.token = None
        if foyer_account.token_file.exists():
            foyer_account.token_file.unlink()
        self._authenticate(foyer_account)

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None
