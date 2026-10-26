# CI scope and usage controls

This workflow saves redundant GitHub Actions work without changing the release
gate, scoring behavior, coverage threshold, or production deployment.

## Which runs remain full

- Every push to `master`, including a merged documentation PR.
- The existing weekly scheduled run.
- PRs containing code, tests, workflows, dependencies, configuration, operations
  files, or any path outside the explicit documentation allowlist.
- Any PR whose changes or commit identity cannot be established safely.

Full validation retains Ruff, shell syntax checks, the function-length gate,
the isolated complete Python 3.11 test suite with its existing coverage floor,
and the production image build and installed-image checks.

## Documentation-only PRs

Only regular non-executable Markdown files in the explicit allowlist can
qualify: root `README.md`, root `CONTRIBUTING.md`, and Markdown under `docs/`.
`docs/lint-baseline.md` is excluded because an existing test reads its content.
Markdown under application, test, operations, or workflow paths is not exempt.
Renames, deletions, file-type or executable-mode changes are conservatively
treated as full changes.

The classifier inspects the complete PR diff from its merge base, not merely
the last commit. Missing metadata, missing Git objects, ambiguous history,
empty diffs, and command failures do not grant the exemption. It is loaded
from the PR base revision, so a PR cannot change the classifier to skip its
own tests. The first PR introducing the classifier necessarily runs full CI.
The default PR merge checkout is retained. The merge tree must equal the
reviewed head tree to qualify; any additional merge content selects full CI.

The lightweight path checks document encoding/content and whitespace and runs
the standard-library scope regressions. It does not install application
dependencies, run the full application tests, or build an image. Its summary
states those exclusions explicitly; a skipped full test job is not a claim
that the full suite ran. The `lint` and `test (3.11)` check names stay stable,
and the workflow has no path-level trigger exclusion that would leave checks
pending.
The former one-entry Python 3.11 matrix is expressed as a single Python 3.11
job, retaining the same artifact name and commands, so GitHub does not append
an extra matrix suffix to the explicit check name.
Only a successful lightweight job can skip the full job. Failed or missing
classification/check results select full validation unless the run itself has
been cancelled.

## Superseded runs

Only runs for the same PR share a concurrency group and may replace a run
already in progress. Other PRs do not cancel each other. Master and scheduled
runs use their unique run IDs and are not cancelled by subsequent runs.
GitHub controls dispatch ordering; this is not a guarantee that the newest
commit always starts first.

Both paths still consume some runner time. These changes cannot restore
already-used minutes or bypass an exhausted account quota. They do not change
the paid budget, runner size, release policy, or separate performance-validation
workflows. No automatic merge or deployment is introduced.
