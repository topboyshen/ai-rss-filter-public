import json
from datetime import datetime

import pytest

from modules import verified_close as source

DATE = '2026-09-03'
NOW = int(datetime.fromisoformat('2026-09-11T08:00:00+08:00').timestamp())


def _rows():
    return [
        {'day': '2026-09-02 15:00:00', 'close': '8.900'},
        {'day': '2026-09-03 14:00:00', 'close': '9.090'},
        {'day': '2026-09-03 15:00:00', 'close': '9.010'},
        {'day': '2026-09-04 15:00:00', 'close': '9.900'},
    ]


class Response:
    def __init__(self, url, payload):
        self.url, self.payload = url, payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self.url

    def read(self, limit):
        assert limit == source.MAX_BYTES + 1
        return self.payload


@pytest.mark.parametrize('symbol', ['bj920021', '920021.BJ', 'sh600000', 'sz000001'])
def test_secondary_exact_close_uses_real_provider_timestamp(symbol):
    calls = []

    def opener(request, timeout):
        calls.append(request.full_url)
        assert timeout == 15
        return Response(request.full_url, json.dumps(_rows()).encode())

    rows = source.fetch_sina_close_prices([symbol], trade_date=DATE, now_ts=NOW, opener=opener)
    assert len(rows) == 1
    row = rows[0]
    assert row['close_price'] == 9.01 and row['prev_close'] == 8.9
    assert row['ts'] == int(datetime.fromisoformat('2026-09-03T15:00:00+08:00').timestamp())
    assert row['source_trade_date'] == DATE
    assert 'scale=60' in calls[0] and 'datalen=240' in calls[0]
    assert 'open_price' not in row and 'volume' not in row


@pytest.mark.parametrize('day', ['2026-09-03', '2026-09-03 14:59:59', '2026-09-04 15:00:00'])
def test_no_date_only_intraday_or_shifted_close(day):
    rows = _rows()[:1] + [{'day': day, 'close': '9.01'}]
    assert source._close_record('bj920021', rows, DATE, NOW) is None


def test_future_price_and_missing_previous_close_are_not_repaired():
    close = _rows()[2]
    assert source._close_record('bj920021', [close], DATE, NOW) is None
    before = int(datetime.fromisoformat('2026-09-03T14:59:59+08:00').timestamp())
    assert source._close_record('bj920021', _rows(), DATE, before) is None


def test_missing_intervening_session_does_not_shift_previous_close():
    rows = _rows()
    rows[0]['day'] = '2026-09-01 15:00:00'
    assert source._close_record('bj920021', rows, DATE, NOW) is None


@pytest.mark.parametrize('price', ['NaN', 'Infinity', '-1', '0', True])
def test_invalid_close_rejected(price):
    rows = _rows()
    rows[2]['close'] = price
    with pytest.raises(ValueError):
        source._close_record('bj920021', rows, DATE, NOW)


def test_duplicate_day_rejected_and_order_does_not_change_result():
    rows = _rows()
    record = source._close_record('bj920021', rows[::-1], DATE, NOW)
    assert record is not None and record['close_price'] == 9.01
    with pytest.raises(ValueError, match='duplicate'):
        source._close_record('bj920021', rows + [rows[2]], DATE, NOW)


@pytest.mark.parametrize('mode', ['redirect', 'oversize', 'json'])
def test_response_safety(mode):
    def opener(request, **kwargs):
        url = 'https://other.invalid' if mode == 'redirect' else request.full_url
        raw = b'{' if mode == 'json' else b'x' * (source.MAX_BYTES + 1)
        return Response(url, raw)
    with pytest.raises(ValueError):
        source.fetch_sina_close_prices(['bj920021'], trade_date=DATE, now_ts=NOW, opener=opener)


def test_invalid_symbol_preflight_makes_no_network_request():
    with pytest.raises(ValueError):
        source.fetch_sina_close_prices(['bj920021', 'sh600000&evil=1'], trade_date=DATE,
                                      opener=lambda *a, **kw: pytest.fail('no request'))
