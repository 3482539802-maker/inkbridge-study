# HarmonyOS capture application

ArkTS lifecycle/UI plus native input, clock and geometry transport. Target SDK:
HarmonyOS API 24. Build from the repository using `build-harmony.ps1` and your
own DevEco installation/signing. See `docs/RUNBOOK.md`.

Users personally authorize/start/stop capture. Do not drive Notes UI, overlay
feedback, record audio, or renew stopped consent. No credential or signed package
is included; `build-profile.example.json5` has an empty signing configuration.
