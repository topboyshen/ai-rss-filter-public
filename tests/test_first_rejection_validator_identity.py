"""validator_version must cover every file the verdict path reads from."""

import ast
import shutil
from pathlib import Path

import pytest

import services.first_rejection_retention as retention
from services.first_rejection_retention import (
    VALIDATOR_FILES,
    validator_identity,
    validator_identity_for,
)

ROOT = Path(__file__).resolve().parents[1]
# Telemetry sinks on the verdict path: they record, they never decide.
SINKS = {"record_score_validation", "capture_validation_attempt", "get_metrics", "logger"}
ENTRY = (("core/scorer.py", "Scorer._validate_scoring_attempt"),
         ("core/scorer.py", "Scorer._parse_score_result"),
         ("core/scorer.py", "Scorer._parse_score_result_with_detail"))


def import_table(nodes) -> dict:
    table = {}
    for node in nodes:
        if isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                table[a.asname or a.name] = ("from", node.module, a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                table[(a.asname or a.name).split(".")[0]] = ("mod", a.name, None)
    return table


def verdict_files(root: Path, entries=ENTRY) -> set:
    """AST closure: project files that functions/constants on the verdict path come from."""
    parsed = {}

    def load(rel):
        if rel not in parsed:
            tree = ast.parse((root / rel).read_text(encoding="utf-8"))
            defs, imports, methods = {}, {}, {}
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    defs[node.name] = node
                    methods[node.name] = {n.name: n for n in node.body
                                          if isinstance(n, ast.FunctionDef)}
                    for n in node.body:
                        if isinstance(n, (ast.Assign, ast.AnnAssign)):
                            for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                                if isinstance(t, ast.Name):
                                    defs[f"{node.name}.{t.id}"] = n
                elif isinstance(node, ast.FunctionDef):
                    defs[node.name] = node
                elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                    for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                        for n in ast.walk(t):
                            if isinstance(n, ast.Name):
                                defs[n.id] = node
            imports.update(import_table(tree.body))
            parsed[rel] = (defs, imports, methods)
        return parsed[rel]

    def modfile(module):
        base = module.replace(".", "/")
        for cand in (f"{base}.py", f"{base}/__init__.py"):
            if (root / cand).is_file():
                return cand
        return None

    seen, files = set(), set()

    def visit(rel, name):
        if (rel, name) in seen or name.split(".")[-1] in SINKS:
            return
        seen.add((rel, name))
        files.add(rel)
        defs, imports, methods = load(rel)
        cls, _, meth = name.partition(".")
        node = methods.get(cls, {}).get(meth) if meth else defs.get(name)
        node = node or defs.get(name)
        if node is None or isinstance(node, ast.ClassDef):
            return  # a bare class reference contributes its file; methods are followed on use
        local = import_table(ast.walk(node))  # imports inside this function only
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name):
                ref(rel, sub.id, local)
            elif isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name):
                owner = sub.value.id
                if owner == "self" and meth:
                    visit(rel, f"{cls}.{sub.attr}")
                elif {**imports, **local}.get(owner, ("",))[0] == "mod":
                    target = modfile({**imports, **local}[owner][1])
                    if target:
                        visit(target, sub.attr)
                elif owner in defs and isinstance(defs[owner], ast.ClassDef):
                    visit(rel, f"{owner}.{sub.attr}")

    def ref(rel, ident, local):
        defs, imports, _ = load(rel)
        if ident in SINKS:
            return
        imports = {**imports, **local}
        if ident in imports:
            kind, module, original = imports[ident]
            target = modfile(module)
            if target and kind == "from":
                visit(target, original)
        elif ident in defs:
            visit(rel, ident)

    for rel, name in entries:
        visit(rel, name)
    return files


def test_the_verdict_path_reads_only_files_in_the_hash_list():
    assert verdict_files(ROOT) <= set(VALIDATOR_FILES)


def test_the_guard_notices_an_unlisted_dependency(tmp_path):
    (tmp_path / "core").mkdir()
    (tmp_path / "extra.py").write_text("THRESHOLD = 3\n")
    (tmp_path / "core" / "scorer.py").write_text(
        "from extra import THRESHOLD\n"
        "class Scorer:\n"
        "    def _validate_scoring_attempt(self, raw):\n"
        "        return raw > THRESHOLD\n"
        "    def _parse_score_result(self, raw):\n"
        "        return raw\n"
        "    def _parse_score_result_with_detail(self, raw):\n"
        "        return raw\n")
    assert verdict_files(tmp_path) == {"core/scorer.py", "extra.py"}
    assert not verdict_files(tmp_path) <= set(VALIDATOR_FILES)


def test_the_guard_ignores_telemetry_sinks_but_follows_constants(tmp_path):
    (tmp_path / "core").mkdir()
    (tmp_path / "db").mkdir()
    (tmp_path / "db" / "score_quality.py").write_text("def record_score_validation(**k): pass\n")
    (tmp_path / "core" / "scorer.py").write_text(
        "from db.score_quality import record_score_validation\n"
        "class Scorer:\n"
        "    def _validate_scoring_attempt(self, raw):\n"
        "        record_score_validation(a=1)\n"
        "        return raw\n"
        "    def _parse_score_result(self, raw):\n"
        "        return raw\n"
        "    def _parse_score_result_with_detail(self, raw):\n"
        "        return raw\n")
    assert verdict_files(tmp_path) == {"core/scorer.py"}


def test_provenance_and_evidence_are_in_the_list():
    assert {"core/scorer.py", "core/score_evidence.py", "scoring_provenance.py"} <= set(
        VALIDATOR_FILES)
    version, files = validator_identity()
    assert set(files) == set(VALIDATOR_FILES) and version.startswith("v2:")


@pytest.mark.parametrize("rel", VALIDATOR_FILES)
def test_changing_only_one_dependency_changes_the_version(tmp_path, rel):
    for name in VALIDATOR_FILES:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / name, tmp_path / name)
    base_version, base_files = validator_identity_for(tmp_path)
    assert (base_version, base_files) == validator_identity()
    target = tmp_path / rel
    text = target.read_text(encoding="utf-8")
    if rel == "scoring_provenance.py":
        changed = text.replace('"来源未显示明确投资催化"', '"来源未显示明确投资催化。"', 1)
        assert changed != text  # the fixed-neutral constant itself
    else:
        changed = text + "\n# dependency changed\n"
    target.write_text(changed, encoding="utf-8")
    version, files = validator_identity_for(tmp_path)
    assert version != base_version
    assert [k for k in files if files[k] != base_files[k]] == [rel]


def test_a_missing_dependency_means_unknown_not_a_partial_hash(tmp_path):
    for name in VALIDATOR_FILES[:-1]:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / name, tmp_path / name)
    assert validator_identity_for(tmp_path) == ("unknown", {})


def test_rows_with_an_unknown_validator_are_not_replayable():
    assert retention.validator_identity()[0] != "unknown"
