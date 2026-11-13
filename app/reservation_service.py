"""Bridge for account administration while that route still owns a JSON transaction."""

from .persistence.json import document_work
from .services.circulation import cancel_disabled_in_work


def cancel_disabled(document, user_id, actor_id, timestamp, *, namespace):
    with document_work(document) as work:
        cancel_disabled_in_work(work, user_id, actor_id, timestamp, namespace)
