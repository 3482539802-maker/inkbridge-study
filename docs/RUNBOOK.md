# Windows setup and operation

Use Python 3.11+, PowerShell 7 and a checkout path without spaces.
Install `requirements.txt`, `requirements-visual.txt`, and optionally
`requirements-materials.txt` for computer-only solution HTML. PDF dependencies
have their own licensing terms; see `THIRD_PARTY_NOTICES.md`.

## Device build

Install official DevEco Studio/HarmonyOS API 24 SDK. Build with:

```powershell
./build-harmony.ps1 -StudioPath 'D:/DevEco Studio'
```

The script creates local `app/build-profile.json5` from the unsigned example
when absent. Configure your own developer signing/ACL and device in the IDE.
Never reuse another person's certificate or assume a signed debug package is
portable. Input monitoring permission and firmware behavior require independent
verification. No signed package or credential is distributed.

## Materials

Use `python scripts/course_workbook.py --help` to propose, review and build a
question plan. `scripts/hinote_workbook.py --help` packages a reviewed manifest.
Send to the public Docs directory with `scripts/tablet_files.py`; the user imports
Notes themselves. Keep computer solutions separate from answer-free workbooks.

## Local services

Customize the clean config templates using your own absolute paths and prepared
manifests/bindings. Store production config at `output/learning/runtime.json`;
see `docs/COURSE_AUTOMATION.md`. Do not overwrite a working config with a template.

```powershell
./start-study.ps1 -Status
./connect-tablet.ps1 -CheckOnly
```

Missing config/service means setup is incomplete. Inspect processes and receiver
sessions before starting anything. Only when requested and no instance exists:
`./start-study.ps1`; raw-only receiver: `./start-PC.ps1 -NoBrowser`.
Connection recovery: `./connect-tablet.ps1 -NoLaunch`, using the remembered local
endpoint or a user-supplied current target. Default connection behavior may launch
the app, so unattended callers must use the appropriate explicit switch.

User opens capture, starts and authorizes, returns to Notes, and stops when done.
Never restart/install over active capture. Check fresh status, ACK gaps, clocks,
unknown reasons and numeric final audit; no new screenshots after stop.

## Offline validation

`verify_workspace.py --group core` covers unittest modules, not all standalone
scripts/builds. Run receiver/replay checks separately. Model tests require an
explicit existing torch interpreter via `--recognition-python`; no model weights
are downloaded by the verifier. Private-fixture skips are reported explicitly.
