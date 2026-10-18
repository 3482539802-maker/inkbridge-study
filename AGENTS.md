# Contributor instructions

Read `docs/HANDOFF.md`, `docs/WORKFLOW.md`, then `docs/WORKSTREAMS.md`.
Use this checkout as the root; never assume a maintainer's local paths or data.

Keep Huawei Notes as the writing app. Users import/export, start, authorize and
stop capture themselves. Never drive tablet UI, switch apps, change pages,
scroll/zoom, renew a revoked grant, or show feedback overlays. No install or
service replacement during active capture. Inspect current processes, fresh
receiver sessions and HDC before live work. Use `connect-tablet.ps1 -CheckOnly`
for inspection, `-NoLaunch` for route recovery; don't create duplicate observers.

Never edit, truncate, delete or upload canonical `output/ink/sessions/`.
Preserve clock plus full history, user originals, active reports, labels,
model weights and environments. Temporary authorized geometry pixels must be
deleted promptly; post-stop screenshots are not writing-time evidence.
Keep signing config and API keys local, never print/commit them. Commit only
clean example templates. Never commit user data, browser sessions, databases,
signed packages, device identifiers or model binaries, even to a private repo.

Keep candidate/extrapolated/motion/unknown distinct. No stale mapping or invented
coordinates to improve coverage. Gesture/tool candidates are not verified edits.
Raw overlays are not final answers. Contact duration is not thinking time;
pauses/stroke counts are not mastery. Source, processing, recognition and response
latencies must remain distinct. A build or image residual is not physical accuracy.

Use isolated `codex/` source branches/worktrees and separate test ports/directories.
Don't reset or stage another contributor's work. Only one integration owner may
deploy live services. Run module checks, `verify_workspace.py --group core`,
receiver replay checks when relevant, and `build-harmony.ps1` for ArkTS/native.
Use explicit existing torch environments for optional model checks.

On Windows prefer cmd/login:false for simple unquoted commands, PowerShell 7
for quoting/metacharacters and filesystem moves/deletion. Prefer rg and scripts.
Do not upload this checkout's old/private history into the public repository.
