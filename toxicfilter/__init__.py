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

from . import webhooks
from .client import VERSION, Client, Transport, UrllibTransport
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

__all__ = [
    "VERSION",
    "AuthenticationError",
    "BatchResult",
    "Client",
    "InvalidRequest",
    "NotFound",
    "QuotaExhausted",
    "RateLimited",
    "ServerError",
    "ToxicFilterError",
    "Transport",
    "UrllibTransport",
    "Verdict",
    "webhooks",
]

__version__ = VERSION
