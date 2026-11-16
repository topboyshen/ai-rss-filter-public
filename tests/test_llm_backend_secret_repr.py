"""P2-8: LLMBackend repr must not expose the API key."""

from core.scorer import LLMBackend

SECRET = "sk-test-secret-value"


def test_backend_repr_omits_api_key_and_runtime_secrets():
    backend = LLMBackend(
        name="primary",
        api_key=SECRET,
        base_url="https://api.example.test/v1",
        model="example-model",
    )
    rendered = repr(backend)
    assert SECRET not in rendered
    assert "api_key" not in rendered
    # Public configuration stays visible for diagnostics.
    assert "name='primary'" in rendered
    assert "https://api.example.test/v1" in rendered
    assert "model='example-model'" in rendered
    # Runtime handles/counters were already excluded and must stay excluded.
    assert ", client=" not in rendered
    assert ", lock=" not in rendered
    assert ", fail_count=" not in rendered
