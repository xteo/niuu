# Public source and image review

Audit date: 2026-09-22. Target: `xteo/niuu`, release branch
`forge/ux-improvement`.

## Scope and findings

A fresh unauthenticated mirror contained 1,540 reachable commits, including public
branches, tags and pull-request refs. TruffleHog 3.97.5 and Gitleaks 8.30.1 scanned
history with offline detectors. Credential candidates were not sent to providers
for verification. The audit also inspected tracked configuration, live-data
fixtures, deployment notes, image metadata and screenshot text.

Gitleaks reported 32 historical occurrences; TruffleHog reported 98. Review found
inert test/example values, a test-only signing key, and detector false positives
(including a Python identifier and part of an issue URL). No operational provider
credential was confirmed in these findings. This is a bounded review, not a claim
that automated tools prove the absence of all sensitive information.

The privacy review did find real deployment addresses, personal paths, session
identifiers and runtime inventory in documentation and captures. The release
branch now uses documentation addresses and generic identities. A gateway fixture
retains only the protocol/order/timing properties used by its tests; its live
inventory and conversation payloads are removed. Two documentation screenshots
with deployment details and an unnecessary temporary screenshot are removed.
The APNs test generates its signing key at runtime.

The exact 32 reviewed historical Gitleaks fingerprints are recorded in
`.gitleaksignore`; there is no broad exclusion of test files or secret detectors.
Raw scanner reports and the pre-curation snapshot stay outside the repository.

## Prevention and packaging

- `.gitignore` excludes machine-local configuration and captures; raw log captures
  are no longer explicitly re-included.
- `scripts/security/check_public_tree.py` rejects tracked runtime state, private
  PEM keys and personal tailnet hostnames without printing matched values.
- `.dockerignore` allows only build inputs and excludes credentials, Git metadata,
  dependencies, local state, captures and generated outputs.
- The publication workflow checks the source, builds each architecture locally on
  its runner, tests the packaged identity/UI, scans image layers, then publishes.
- Images receive immutable source identity as package data. Node configuration,
  credentials, user data and workspaces are supplied only at runtime.

## Historical privacy remediation

An ordinary cleanup commit does **not** erase earlier public copies. Earlier refs
also contain raw conversation captures under `.skuld/`, an older full gateway
capture, old versions of live web configuration, and the removed screenshots.
They remain accessible until a separately coordinated history cleanup occurs.

The private audit inventory identifies the affected blobs and refs. The proposed
cleanup removes raw runtime captures and the identified images throughout public
history, sanitizes old host/path identifiers and preserves the current sanitized
protocol fixture. Rewriting published branches and tags changes commit IDs and
requires coordinated force-updates, clone/worktree resynchronization, and review
of open PRs. GitHub's cached/PR refs and third-party clones cannot be erased by a
normal Git push; GitHub support may be needed for retained sensitive objects.

The public container is built from the curated source tree; it never includes Git
history. Publishing it is independent of completing that disruptive history purge.
