import ast
from pathlib import Path

import pytest

import scripts.check_lint as check_lint
from scripts.check_lint import baseline_counts, finding_counts, pinned_ruff_version


def test_lint_baseline_keeps_location_independent_counts(tmp_path):
    finding = {
        "filename": str(tmp_path / "app.py"),
        "code": "F401",
        "message": "unused import",
        "location": {"row": 1},
    }
    moved = {**finding, "location": {"row": 100}}
    assert finding_counts([finding], tmp_path) == finding_counts([moved], tmp_path)
    allowed = finding_counts([finding], tmp_path)
    current = finding_counts([finding, moved], tmp_path)
    assert sum((current - allowed).values()) == 1


def test_lint_baseline_cannot_hide_a_new_rule_message_or_file(tmp_path):
    original = {
        "filename": str(tmp_path / "app.py"),
        "code": "F401",
        "message": "unused import",
    }
    allowed = finding_counts([original], tmp_path)
    for changed in (
        {"code": "F821"},
        {"message": "different import"},
        {"filename": str(tmp_path / "new.py")},
    ):
        assert (
            sum(
                (finding_counts([{**original, **changed}], tmp_path) - allowed).values()
            )
            == 1
        )
    assert sum((allowed - finding_counts([], tmp_path)).values()) == 1


@pytest.mark.parametrize("count", [0, -1, True, "1"])
def test_lint_baseline_rejects_invalid_counts(count):
    with pytest.raises(ValueError):
        baseline_counts(
            {
                "schema": 1,
                "ruff_version": "1.2.3",
                "findings": [
                    {
                        "path": "app.py",
                        "code": "F401",
                        "message": "unused",
                        "count": count,
                    },
                ],
            },
            "1.2.3",
        )


def _baseline(version, findings):
    return {"schema": 1, "ruff_version": version, "findings": findings}


def test_recorded_findings_require_the_pinned_ruff_version():
    entry = {"path": "app.py", "code": "F401", "message": "unused", "count": 1}
    assert sum(baseline_counts(_baseline("1.2.3", [entry]), "1.2.3").values()) == 1
    with pytest.raises(ValueError, match="captured with Ruff 1.2.3"):
        baseline_counts(_baseline("1.2.3", [entry]), "1.2.4")


def test_empty_baseline_allows_nothing_under_any_pinned_version():
    assert baseline_counts(_baseline("1.2.3", []), "1.2.4") == {}


@pytest.mark.parametrize(
    "payload",
    [
        {"schema": 2, "ruff_version": "1.2.3", "findings": []},
        {"schema": 1, "findings": []},
        {"schema": 1, "ruff_version": "1.2.3"},
    ],
)
def test_lint_baseline_rejects_malformed_metadata(payload):
    with pytest.raises(ValueError):
        baseline_counts(payload, "1.2.3")


def test_ruff_pin_is_read_from_lint_requirements(tmp_path):
    requirements = tmp_path / "requirements-lint.txt"
    requirements.write_text("# dev only\nruff==1.2.3  # pinned\n", encoding="utf-8")
    assert pinned_ruff_version(requirements) == "1.2.3"


@pytest.mark.parametrize(
    "content",
    ["", "ruff>=1.2\n", "ruff==1.2.3\nruff==1.2.4\n", "Ruff~=1.2.3\n"],
)
def test_ruff_pin_must_be_exact_and_unique(tmp_path, content):
    requirements = tmp_path / "requirements-lint.txt"
    requirements.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        pinned_ruff_version(requirements)


def test_checker_refuses_a_ruff_that_differs_from_the_pin(monkeypatch, capsys):
    monkeypatch.setattr(check_lint, "pinned_ruff_version", lambda: "1.2.3")
    monkeypatch.setattr(check_lint, "installed_ruff_version", lambda: "1.2.4")
    monkeypatch.setattr(
        check_lint.subprocess,
        "check_output",
        lambda *a, **k: pytest.fail("must not lint with an unpinned Ruff"),
    )
    assert check_lint.main([]) == 2
    assert "pins 1.2.3" in capsys.readouterr().err


def test_lint_baseline_rejects_outside_paths(tmp_path):
    with pytest.raises(ValueError):
        finding_counts(
            [
                {
                    "filename": str(Path(tmp_path).parent / "outside.py"),
                    "code": "F401",
                    "message": "unused",
                }
            ],
            tmp_path,
        )


def test_redefinition_reference_lines_do_not_change_the_baseline(tmp_path):
    original = {
        "filename": str(tmp_path / "app.py"),
        "code": "F811",
        "message": "Redefinition of unused `database` from line 256",
    }
    moved = {**original, "message": "Redefinition of unused `database` from line 254"}
    assert finding_counts([original], tmp_path) == finding_counts([moved], tmp_path)


def test_legacy_postgres_script_remains_python311_parseable():
    source = Path(__file__).resolve().parents[1] / "scripts" / "migrate_to_postgres.py"
    ast.parse(source.read_text(encoding="utf-8"), feature_version=(3, 11))
