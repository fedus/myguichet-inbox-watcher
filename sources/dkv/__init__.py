"""DKV/Lalux EasyApp source plugin."""

from sources.dkv.source import (
    DEFAULT_PAGE_LIMIT,
    DkvDocumentSource,
    collect_available_documents,
    collect_contract_documents,
    collect_invoice_messages,
    collect_treated_refunds,
    document_from_contract_message,
    document_from_invoice_detail,
    documents_from_refund_detail,
)

__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "DkvDocumentSource",
    "collect_available_documents",
    "collect_contract_documents",
    "collect_invoice_messages",
    "collect_treated_refunds",
    "document_from_contract_message",
    "document_from_invoice_detail",
    "documents_from_refund_detail",
]
