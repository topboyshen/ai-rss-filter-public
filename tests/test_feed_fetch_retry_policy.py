"""Feed fetches must not retry permanent 4xx answers, and backoff needs jitter.

``fetch_feed`` used to retry every exception except ``PermissionError``.  A 404
or 403 therefore cost three requests and six seconds of linear sleep per feed,
on every refresh, for an answer that can never change.  Retrying 5xx/408/429 is
still correct; the backoff simply needs to spread concurrent feeds out.
"""

import time
import urllib.error

import pytest

import routes.security as security
import utils.helpers as helpers

FEED_URL = "https://public.example/rss"
CFG = {"feeds": [{"url": FEED_URL}]}


class _Response:
    def __init__(self, payload: bytes = b"<rss></rss>"):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self, _limit=None):
        return self._payload


class _Opener:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def open(self, *_args, **_kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return _Response(outcome)


def _http_error(status: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(FEED_URL, status, "failure", {}, None)


@pytest.fixture
def fetch_env(monkeypatch):
    """Install a fake opener and capture sleeps instead of waiting."""
    monkeypatch.setattr(security, "pin_request_target", lambda *_a, **_k: None)
    sleeps = []
    monkeypatch.setattr(time, "sleep", sleeps.append)

    def install(*outcomes):
        opener = _Opener(outcomes)
        monkeypatch.setattr(helpers, "build_opener", lambda *_handlers: opener)
        return opener, sleeps

    return install


def _fetch(**kwargs):
    return helpers.fetch_feed(FEED_URL, timeout=1, cfg=CFG, **kwargs)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 451])
def test_permanent_4xx_is_not_retried(fetch_env, status):
    opener, sleeps = fetch_env(_http_error(status))

    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _fetch(max_attempts=3, backoff_base=2)

    assert excinfo.value.code == status
    assert opener.calls == 1
    assert sleeps == []


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503])
def test_retryable_status_is_retried(fetch_env, status):
    opener, sleeps = fetch_env(_http_error(status), b"<rss/>")

    assert _fetch(max_attempts=3, backoff_base=2) == b"<rss/>"

    assert opener.calls == 2
    assert len(sleeps) == 1


def test_connection_errors_are_still_retried(fetch_env):
    opener, sleeps = fetch_env(OSError("transient connection failure"), b"<rss/>")

    assert _fetch(max_attempts=3, backoff_base=2) == b"<rss/>"

    assert opener.calls == 2
    assert len(sleeps) == 1


def test_retries_stop_after_max_attempts(fetch_env):
    opener, sleeps = fetch_env(*[_http_error(500) for _ in range(3)])

    with pytest.raises(urllib.error.HTTPError):
        _fetch(max_attempts=3, backoff_base=2)

    assert opener.calls == 3
    assert len(sleeps) == 2


@pytest.mark.parametrize(
    "jitter,expected",
    [
        pytest.param(0.5, [1.0, 2.0], id="shortest"),
        pytest.param(1.0, [2.0, 4.0], id="longest"),
    ],
)
def test_backoff_is_jittered_within_half_to_full_delay(
    fetch_env, monkeypatch, jitter, expected
):
    class _FixedRandom:
        def uniform(self, low, high):
            assert (low, high) == (0.5, 1.0)
            return jitter

    opener, sleeps = fetch_env(*[_http_error(500) for _ in range(3)])
    monkeypatch.setattr(helpers, "random", _FixedRandom())

    with pytest.raises(urllib.error.HTTPError):
        _fetch(max_attempts=3, backoff_base=2)

    assert opener.calls == 3
    assert sleeps == expected
