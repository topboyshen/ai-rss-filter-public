# Incremental lint baseline

The development-only tool is [Ruff](https://pypi.org/project/ruff/). Its exact
version is pinned **only** in `requirements-lint.txt` (`ruff==X.Y.Z`), which is
the single source of truth. `scripts/check_lint.py` reads that pin and exits
with status 2 before linting if the installed Ruff differs, so every run of the
gate uses one exact version. `ruff.toml` deliberately has no `required-version`:
an exact copy there would duplicate the pin (and broke Dependabot's Ruff bump in
#151), while a range would let ad-hoc runs drift. Do not copy the version into
other files; a test enforces this. Production dependencies and scoring files are
unchanged. The target syntax is Python 3.11, matching the existing Linux test
environment.

Use the same commands locally and in the existing CI workflow:

```sh
.venv-release-gate/bin/python -m pip install -r requirements-lint.txt
.venv-release-gate/bin/python scripts/check_lint.py
```

The tool scans tracked and non-ignored new Python files without writing a cache
or fixing source. `.ci/ruff-baseline.json` initially records findings from the
unmodified `4525aef` source, with the Ruff version recorded in its
`ruff_version` field and this rule set. A green baseline check means **no
additional debt**, not zero Ruff findings.

Diagnostic text can change between Ruff releases, so a baseline with recorded
findings is valid only when its `ruff_version` equals the pin. While `findings`
is empty it allows nothing under any version, so a reviewed Ruff upgrade only
edits `requirements-lint.txt`; CI then proves no new findings appear.

The 2026-09-10 cleanup removes the remaining 77 findings and all corresponding
allowances. The checked-in `findings` list is now empty, so any finding in the
configured rule set fails this gate. This does not claim that repository-wide
formatting or function-length debt has been cleared. Compatibility re-exports
and required initialization calls are retained explicitly; no rule is disabled.

Allowances bind file, rule, diagnostic message and occurrence count. Line
movement (including an F811 diagnostic's referenced line) does not count as a
new finding. A new file, variable name, rule or duplicate occurrence does. Any
new finding fails CI. Fixing an old finding also requires removing its obsolete
allowance in the same small cleanup change, so resolved debt cannot silently
return. The checker never updates allowances automatically.

To inspect a cleanup diff, `python scripts/check_lint.py --report` prints the
current normalized findings. Review and remove resolved entries; do not raise
allowances just to make a failing change pass. The metadata source revision
records the original capture, not a claim that all later source is unchanged.

Formatting is deliberately separate: `python -m ruff format --check --no-cache .`
reports existing formatting debt but is not yet a whole-repository blocker.
Format new files and future small cleanup scopes; do not combine broad format
rewrites, scoring changes, and mapping rules in one patch. Do not format the
scoring-fingerprint source files casually: their full bytes affect the contract.

Configuration reference: [Ruff configuration](https://docs.astral.sh/ruff/configuration/).
