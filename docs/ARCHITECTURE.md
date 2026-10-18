# Architecture

`app/` contains ArkTS lifecycle/consent and C++ input/screen-clock/geometry
transport. On tested firmware input monitoring requires a user-authorized
screen-capture session as well as appropriate Input Kit permissions. Microphone
and system sound stay off; audioRecording compatibility is not audio capture.

`ink_receiver.py` persists canonical journals before ACK; `ink_protocol.py` and
`ink_sessions.py` manage framing/backfill. Raw transport is independent of images.
`session_pipeline.py` schedules authorized geometry; `coordinate_stream.py`
incrementally indexes bounded raw windows into a single-writer SQLite store.
Clock history, page identities, registration support and barriers accompany
coordinates. Raw is never rewritten to hide unknowns.

`course_workbook.py` records source crops/hashes and answer-free workbooks;
`hinote_workbook.py` produces native PDF background archives. UUIDs must be
verified after import. `course_learning.py`, `course_review.py`,
`course_runtime.py`, `course_tutor.py` and `recognition_chat.py` manage local
activities, immutable requests, review provenance, validation and knowledge UI.
Downstream readers do not rewrite upstream databases. User feedback enters
through validated inboxes/new revisions, not ad-hoc SQL edits.

`skills/inkbridge-study/scripts/bridge.py` resolves a checkout through
`INKBRIDGE_HOME`, user-local config or package location, and only delegates a
read-only status check. The skill does not bundle a second production server.

Default local ports: raw TCP 18765, raw HTTP 18766, learning view 18767,
geometry transport 18769, legacy review 18770, course dashboard 18771.
Do not expose these services publicly or assume an open port proves freshness.
