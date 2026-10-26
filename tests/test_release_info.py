from services.release_info import release_version


def test_release_version_reports_valid_image_ref(monkeypatch):
    monkeypatch.setenv("AI_FILTER_VERSION", "my-ai-filter:2026.07.18.1")
    assert release_version() == "my-ai-filter:2026.07.18.1"


def test_release_version_rejects_untrusted_or_oversized_text(monkeypatch):
    monkeypatch.setenv("AI_FILTER_VERSION", "<script>alert(1)</script>")
    assert release_version() == "development"
    monkeypatch.setenv("AI_FILTER_VERSION", "a" * 129)
    assert release_version() == "development"
