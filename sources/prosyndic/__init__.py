"""ProSyndic document source plugin."""

from sources.prosyndic.config import DEFAULT_PAGE_LIMIT, prosyndic_account_from_source
from sources.prosyndic.source import (
    ProSyndicDocumentSource,
    collect_documents,
    document_from_payload,
)

__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "ProSyndicDocumentSource",
    "collect_documents",
    "document_from_payload",
    "prosyndic_account_from_source",
]
