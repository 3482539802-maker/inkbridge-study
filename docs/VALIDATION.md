# Distribution validation

The initial source distribution was tested on Windows with Python 3.13:

Follow-up entrypoint audit: 8 routing/freshness checks passed, including refusal
to redirect an existing toolbox, explicit replacement, stale/missing health,
clock anomalies, finite socket timeout and HTTP redirects. An isolated installer
exercise verified repeated updates preserve config, keep a skill backup, and
switch defaults only when explicitly requested. Cross-workspace live status was
read without restarting services or accessing tablet UI. These are targeted
follow-up checks, not a new end-to-end device trial.

- Core offline suite: 508 tests, 504 passed and 4 explicit private-fixture skips.
- Optional isolated recognition adapter suite: 7 tests passed.
- Standalone raw TCP/HTTP pipeline and durable session restart/replay passed.
- Skill frontmatter validation and cross-workspace checkout resolution passed.
- Tracked-path/credential policy and Gitleaks source/history scans passed.

No private source PDFs, raw journals, real handwriting samples or model binaries
were supplied to the clean distribution. Private-fixture skips do not constitute
device validation. Synthetic transform checks measure generated geometry only.

Packaging changes did not alter app/native behavior. No new tablet package was
installed and no new capture trial was requested. The original installed system
remained running; this release does not prove other firmware support, millimeter
accuracy, final edit-state reconstruction, or all-day reliability.

Reproduce core checks with `python scripts/verify_workspace.py --group core`.
An optional host C++ compiler is required for native harnesses. Model tests need
an existing isolated interpreter via `--recognition-python`. Run standalone
receiver/replay and relevant CJS/PowerShell checks separately. ArkTS/native changes
also require the Harmony build and appropriately authorized device validation.

Before future publication, stage only intended source and run
`python scripts/audit_public_release.py`, a full secret scanner against source
and all history, and `git diff --cached --check`. This publication policy is
stricter than ordinary private development and intentionally rejects raw/user data.
