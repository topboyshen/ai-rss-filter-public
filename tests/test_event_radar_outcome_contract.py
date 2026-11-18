"""P4: explain an empty market tab instead of only saying data may be missing."""

from pathlib import Path

from services.event_radar_query import _OUTCOME_CONTRACT_NOTES, _outcome_contract_state


class _Rows:
    def __init__(self, value):
        self.value = value

    def fetchone(self):
        return self.value


class _Conn:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def execute(self, sql, args=()):
        self.calls.append((sql, args))
        return _Rows(self.answers.pop(0))


def test_empty_without_a_selected_signal_names_the_reason():
    conn = _Conn([None])

    state, note = _outcome_contract_state(conn, "event-1", 100)

    assert state == "no_selected_signal"
    assert note == _OUTCOME_CONTRACT_NOTES["no_selected_signal"]
    assert "不生成结果" in note


def test_empty_with_a_contract_mismatch_names_the_reason():
    conn = _Conn([{"id": 1}, None])

    state, note = _outcome_contract_state(conn, "event-1", 100)

    assert state == "contract_mismatch"
    assert note == _OUTCOME_CONTRACT_NOTES["contract_mismatch"]
    assert "不估算补齐" in note


def test_empty_with_only_non_direct_contracts_names_the_reason():
    conn = _Conn([{"id": 1}, {"1": 1}])

    state, note = _outcome_contract_state(conn, "event-1", 100)

    assert state == "no_direct_contract"
    assert note == _OUTCOME_CONTRACT_NOTES["no_direct_contract"]


def test_template_prefers_the_contract_note_over_the_vague_copy():
    template = (Path(__file__).resolve().parents[1] / "templates" / "event_radar.html").read_text(
        encoding="utf-8"
    )
    assert "outcome_contract_note or" in template
