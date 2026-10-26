from ops import ai_filter_ops_check as monitor


def test_metrics_notification_names_fixed_red_reasons():
    results = [monitor.CheckResult("metrics", False, "red gauges=refresh_stale,refresh_degraded")]
    assert monitor._notification_text("failure", results, 1790629201) == (
        "AI-Filter 运维检查失败 | checks=metrics | ts=1790629201"
        " | metrics_reasons=refresh_stale,refresh_degraded"
    )


def test_metrics_notification_never_forwards_arbitrary_detail():
    results = [
        monitor.CheckResult("metrics", False, "red gauges=refresh_stale,token=SECRET,https://secret.invalid"),
        monitor.CheckResult("root_disk", False, "private-path SECRET"),
    ]
    text = monitor._notification_text("failure", results, 12)
    assert "checks=metrics,root_disk" in text
    assert text.endswith("metrics_reasons=refresh_stale")
    assert "SECRET" not in text and "secret.invalid" not in text


def test_non_gauge_failure_and_recovery_remain_unchanged():
    results = [monitor.CheckResult("metrics", False, "request failed SECRET")]
    assert monitor._notification_text("failure", results, 12) == "AI-Filter 运维检查失败 | checks=metrics | ts=12"
    assert monitor._notification_text("recovery", results, 12) == "AI-Filter 运维检查已恢复 | ts=12"


def test_notification_render_does_not_mutate_fingerprint_or_results():
    results = [monitor.CheckResult("metrics", False, "red gauges=refresh_stale,refresh_degraded")]
    before = monitor._fingerprint(results)
    monitor._notification_text("failure", results, 12)
    assert monitor._fingerprint(results) == before
    assert results[0].detail == "red gauges=refresh_stale,refresh_degraded"
