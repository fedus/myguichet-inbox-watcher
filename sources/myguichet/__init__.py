"""MyGuichet source plugin."""

from sources.myguichet.source import (
    COMMUNAL_BILLS_PER_PAGE,
    COMMUNAL_BILL_MESSAGE_KIND,
    REQUESTS_PER_PAGE,
    MyGuichetDocumentSource,
    collect_communal_bills,
    collect_unseen_communications,
)

__all__ = [
    "COMMUNAL_BILLS_PER_PAGE",
    "COMMUNAL_BILL_MESSAGE_KIND",
    "REQUESTS_PER_PAGE",
    "MyGuichetDocumentSource",
    "collect_communal_bills",
    "collect_unseen_communications",
]
