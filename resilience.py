"""Recognising and reporting transient infrastructure failures.

The bot runs on shared hosting, so two things fail regularly and for reasons
that have nothing to do with this code:

* the network path to api.telegram.org drops for a few seconds
  (``httpx.ConnectError`` / ``ReadError`` out of the Updater), and
* MySQL is restarted or killed by a resource limit, so every query for the
  next minute or two fails with ``(2003, "Can't connect to MySQL server")``.

Both recover on their own. What they used to do, though, is emit a full
traceback from every periodic job on every 60-second tick — roughly 180
tracebacks an hour into the log channel — which buried real bugs.

This module provides two things:

``is_transient_infra_error``
    Tells a genuine outage apart from a programming error, so only the former
    is downgraded to a warning.
``TransientErrorReporter``
    Logs the first failure of an outage in full, stays quiet while the outage
    continues, and logs a single recovery line at the end with the number of
    suppressed failures.

Exceptions are matched by class name rather than by importing httpx/pymysql,
so this module stays importable in bare test environments.
"""

import errno
import logging
import time

logger = logging.getLogger(__name__)

# httpx/httpcore transport failures: the request never got an answer. These are
# all "the network went away", never "the code is wrong".
_TRANSIENT_NETWORK_NAMES = {
    "ConnectError", "ConnectTimeout", "ReadError", "ReadTimeout",
    "WriteError", "WriteTimeout", "PoolTimeout", "NetworkError",
    "RemoteProtocolError", "TimeoutException", "ProxyError",
    "IncompleteRead", "LocalProtocolError",
}

# pymysql/aiomysql: the server is unreachable or the socket died mid-query.
# NOTE: ``ProgrammingError``/``IntegrityError`` are deliberately absent — a bad
# query is a bug and must keep its traceback.
_TRANSIENT_DB_NAMES = {
    "OperationalError", "InterfaceError", "InternalError",
}

# Socket-level refusals/resets seen when MySQL is down or a peer disappears.
_TRANSIENT_ERRNOS = {
    errno.ECONNREFUSED,   # 111 — nothing listening (MySQL stopped)
    errno.ECONNRESET,     # 104
    errno.ECONNABORTED,   # 103
    errno.ENETUNREACH,    # 101
    errno.EHOSTUNREACH,   # 113
    errno.ETIMEDOUT,      # 110
    errno.EPIPE,          # 32
}

# get_pool() raises this after exhausting its own retries.
_POOL_EXHAUSTED_MARKER = "Could not connect to MySQL"


def _chain(exc, limit: int = 10):
    """Yield ``exc`` and the exceptions it was raised from.

    aiomysql wraps an ``OSError`` in ``OperationalError``; httpx wraps
    ``httpcore.ConnectError`` in ``httpx.ConnectError``. The interesting class
    is often not the outermost one, so the whole chain is inspected. ``limit``
    guards against a self-referential ``__context__``.
    """
    seen = set()
    while exc is not None and len(seen) < limit and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def is_transient_infra_error(exc: BaseException | None) -> bool:
    """True when ``exc`` is an outage the bot should simply wait out.

    Used to decide between a one-line warning ("MySQL is down, retrying") and
    a full traceback ("this is a bug, look at it").
    """
    if exc is None:
        return False
    for err in _chain(exc):
        name = type(err).__name__
        if name in _TRANSIENT_NETWORK_NAMES or name in _TRANSIENT_DB_NAMES:
            return True
        if isinstance(err, OSError) and err.errno in _TRANSIENT_ERRNOS:
            return True
        # asyncio surfaces a multi-address failure as a plain OSError whose
        # errno is None and whose message lists each attempt.
        if isinstance(err, OSError) and err.errno is None:
            text = str(err)
            if "Connect call failed" in text or "Multiple exceptions" in text:
                return True
        if isinstance(err, RuntimeError) and _POOL_EXHAUSTED_MARKER in str(err):
            return True
    return False


def describe(exc: BaseException | None) -> str:
    """Short, human-readable one-liner for an exception.

    Deliberately not a traceback: an outage line should be readable at a glance
    in the log channel.
    """
    if exc is None:
        return "unknown error"
    root = None
    for root in _chain(exc):
        pass
    text = str(root) or type(root).__name__
    if root is not exc and str(exc):
        text = f"{type(exc).__name__}: {text}"
    return text.replace("\n", " ")[:300]


class TransientErrorReporter:
    """Collapses a burst of identical infrastructure failures into two lines.

    One reporter per job. During an outage the first failure is logged in full
    (warning + traceback, so the cause is on record) and everything after it is
    counted silently until ``repeat_after`` seconds have passed, at which point
    a single reminder is logged. When the next call succeeds, a recovery line
    reports how many failures were swallowed.

    Genuine bugs are never throttled: ``report`` returns False for them so the
    caller can log a normal traceback.
    """

    def __init__(self, name: str, log: logging.Logger | None = None,
                 repeat_after: float = 900.0):
        self.name = name
        self.log = log or logger
        self.repeat_after = repeat_after
        self.failures = 0
        self._last_logged_at = 0.0
        self._monotonic = time.monotonic

    def report(self, exc: BaseException, context: str) -> bool:
        """Handle a failure. Returns True if it was a (throttled) outage.

        A False return means the caller should log the exception itself, with
        a traceback, because it is not an outage.
        """
        if not is_transient_infra_error(exc):
            return False

        self.failures += 1
        now = self._monotonic()
        if self.failures == 1:
            self._last_logged_at = now
            self.log.warning(
                "%s: %s unavailable (%s). Retrying on the next tick; "
                "further identical failures will be summarised.",
                context, self.name, describe(exc), exc_info=exc,
            )
        elif now - self._last_logged_at >= self.repeat_after:
            self._last_logged_at = now
            self.log.warning(
                "%s: %s still unavailable after %d attempts (%s).",
                context, self.name, self.failures, describe(exc),
            )
        return True

    def clear(self, context: str = ""):
        """Note that the dependency answered again."""
        if not self.failures:
            return
        self.log.info(
            "%s: %s reachable again after %d failed attempt(s).",
            context or self.name, self.name, self.failures,
        )
        self.failures = 0
        self._last_logged_at = 0.0
