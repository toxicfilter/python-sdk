"""The official Python client for ToxicFilter.

    from toxicfilter import Client

    tf = Client("tf_live_...")
    verdict = tf.text("Check this message", locales=["en"], surface="comment")

    if verdict.blocked:
        refuse()
    elif verdict.needs_review:
        hold_for_a_person(verdict.id, verdict.reasons)
    else:
        publish()
"""

from .client import Client, Transport, UrllibTransport, VERSION
from .errors import (
    AuthenticationError,
    InvalidRequest,
    NotFound,
    QuotaExhausted,
    RateLimited,
    ServerError,
    ToxicFilterError,
)
from .models import BatchResult, Verdict
from . import webhooks

__all__ = [
    "Client",
    "Transport",
    "UrllibTransport",
    "Verdict",
    "BatchResult",
    "ToxicFilterError",
    "AuthenticationError",
    "QuotaExhausted",
    "RateLimited",
    "InvalidRequest",
    "NotFound",
    "ServerError",
    "webhooks",
    "VERSION",
]

__version__ = VERSION
