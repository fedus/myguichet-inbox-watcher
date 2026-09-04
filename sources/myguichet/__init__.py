"""MyGuichet source plugin."""

from sources.myguichet.source import (
    REQUESTS_PER_PAGE,
    MyGuichetDocumentSource,
    collect_unseen_communications,
)

__all__ = [
    "REQUESTS_PER_PAGE",
    "MyGuichetDocumentSource",
    "collect_unseen_communications",
]
