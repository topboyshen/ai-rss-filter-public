"""Quiet-mode notification thresholds must also cover reused similar entries.

``_effective_notify_threshold`` raises the notify bar during quiet hours for the
main scoring path.  The similarity path sends its own notification, so it must
apply the same raise instead of comparing against the raw feed threshold.
"""

from types import SimpleNamespace

import core.processor as processor


def _ctx(cfg):
    return SimpleNamespace(
        cfg=cfg,
        feed_url="",
        raw_title="甲公司获得订单",
        source_summary="",
        entry=SimpleNamespace(link="https://example.test/new"),
        prompt_name="test_prompt",
        quality_repair=False,
        quality_repair_summary="",
    )


def _drive_similar_persist(monkeypatch, cfg, notify_threshold, score):
    sent = []
    monkeypatch.setattr(processor, "send_telegram_notification", lambda *a: sent.append(a))
    monkeypatch.setattr(processor.DB, "set_cached_entry", lambda *a, **k: {})
    monkeypatch.setattr(processor, "_build_backtest_signal_snapshot", lambda **k: {})
    monkeypatch.setattr(processor, "_persisted_event_identity", lambda *a: (None, None))
    monkeypatch.setattr(processor, "_set_scoring_meta", lambda *a, **k: None)
    monkeypatch.setattr(processor, "_entry_timestamp", lambda *a: 0)
    monkeypatch.setattr(processor, "_source_tier", lambda *a: "")
    similar_info = {
        "score": score,
        "tag": "公司订单",
        "logic": "订单落地改善业绩",
        "direction": "up",
        "similarity": 0.95,
    }
    outcome = processor._persist_similar_entry(_ctx(cfg), similar_info, notify_threshold)
    return sent, outcome


def _cfg():
    return {
        "threshold": {"pass": 7, "notify": 9, "similarity": 0.42},
        "quiet_mode": {
            "enabled": True,
            "notify_threshold_raise": 1.0,
            "schedule": [],
        },
    }


def test_similar_entry_does_not_notify_below_the_quiet_threshold(monkeypatch):
    monkeypatch.setattr(processor, "_is_quiet_mode", lambda cfg: (True, 1.0))

    sent, outcome = _drive_similar_persist(monkeypatch, _cfg(), 9.0, 9.0)

    assert outcome == (None, "similarity")
    assert sent == []


def test_similar_entry_notifies_at_the_quiet_threshold(monkeypatch):
    monkeypatch.setattr(processor, "_is_quiet_mode", lambda cfg: (True, 1.0))

    sent, _outcome = _drive_similar_persist(monkeypatch, _cfg(), 9.0, 10.0)

    assert len(sent) == 1
    assert sent[0][2] == 10.0
    assert sent[0][6] == 10.0


def test_similar_entry_keeps_the_raw_threshold_outside_quiet_hours(monkeypatch):
    monkeypatch.setattr(processor, "_is_quiet_mode", lambda cfg: (False, 0.0))

    sent, _outcome = _drive_similar_persist(monkeypatch, _cfg(), 9.0, 9.0)

    assert len(sent) == 1
    assert sent[0][6] == 9.0


def test_similar_entry_uses_slot_level_raise(monkeypatch):
    monkeypatch.setattr(processor, "_is_quiet_mode", lambda cfg: (True, 0.5))

    sent, _outcome = _drive_similar_persist(monkeypatch, _cfg(), 9.0, 9.4)

    assert sent == []
