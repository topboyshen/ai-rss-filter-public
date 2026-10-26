# Frozen public capacity baseline

The old comparison remains revision
`0f3f56120f95c2ddcec81f5d38b1287cfce2275c`. The public repository does not contain
that commit or its private ancestors. `baseline.tar.xz` supplies the 129 files
needed by that revision's Dockerfile, plus the Compose and Docker ignore files
used by build provenance. It does not contain Git metadata, a database,
deployment evidence, operational correction records, or captured news/model
responses.

`MANIFEST.json` identifies every path, mode, byte count and SHA-256. The native
driver pins both the archive and manifest hashes, verifies all members before
extracting, and rejects missing, modified, extra, duplicate or unsafe members.
It never fetches the old history or silently substitutes the candidate.

Six source files have publication-only example changes, listed in the manifest:
personal service domains become reserved `example.invalid` domains; the three
configured watchlist identifiers become synthetic identifiers; the evidence
directory/comment use an example cohort. Capture settings, scoring thresholds,
and receiver logic remain unchanged. The original Dockerfile, dependency lock,
`app.py`, webhook route and receipt implementation are byte-identical; focused
tests independently pin those hashes.

The existing capacity probe writes its own synthetic configuration **before**
importing the application, forbids network/model/processing calls, and exercises
all original baseline and candidate cases. This package is an audited,
sanitized build context, not a byte-identical Git archive or production image.
Image evidence therefore records `sanitized_minimal_build_context` and a source
identity containing the package hash instead of claiming `revision:clean`.

The public gate preserves native amd64, 768 MiB, 256 PID, original case sizes,
timeouts, synchronization protections and failed-attempt evidence. Baseline
failures are measured, not hidden; all candidate cases must still pass.
