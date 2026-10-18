# Privacy and distribution boundary

Public distribution includes reviewed source, a discoverable skill, unsigned
configuration examples and general documentation. It starts with a new Git
history. Do not merge or publish a private development repository's old history.

Never commit secrets (API tokens, passwords, signing keys/certificates), local
signing profiles, full journals, user PDFs/Notes exports, learner records,
runtime databases/config, browser state, device endpoints/identifiers, signed
HAPs, model weights or virtual environments. A private Git repository also does
not make credentials appropriate to upload. Ignoring files cannot remove secrets
already present in history; review staged files and scan the complete history.

Local locations `materials/`, `output/`, `tools/`, `workspace/`, browser folders
and `app/build-profile.json5` are excluded. Keep credentials in the owner's local
secure configuration. Do not download optional model binaries automatically.

Device screen-capture consent is required on tested firmware for global input
monitoring. Audio stays off. Authorized active-capture geometry pixels are
transient, promptly removed, and never replace raw input. No post-stop screenshots
as writing-time evidence. Users control import/export and capture start/stop.

Inspect publication contents, not just .gitignore. Secret scanning is a useful
check, not proof that any arbitrary future file is safe. Preserve third-party
licenses and verify rights before publishing example materials.
