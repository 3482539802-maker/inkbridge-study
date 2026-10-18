# Modules and ownership

| Module | Main files | Checks |
|---|---|---|
| A materials | course_workbook.py, hinote_workbook.py, course_solution_guide.py | verify_materials_workbook.py |
| B capture/transport | app/, ink_receiver.py, ink_protocol.py, ink_sessions.py | verify_pc_pipeline.py, verify_session_replay.py, native/CJS checks, Harmony build |
| C localization | session_pipeline.py, coordinate_stream.py, visual_registration.py | coordinate/clock/geometry/pipeline checks |
| D edits | edit_events.py, edit_observations.py, edit_spatial_evidence.py | corresponding verify_edit_* |
| E recognition | recognition_ink_input.py, recognition_chat.py, course_tutor.py | corresponding recognition checks; optional isolated model environment |
| F tutoring | course_learning.py, course_review.py, course_knowledge.py | verify_course_workflow.py, verify_course_knowledge.py |
| G runtime | course_runtime.py, start-study.ps1 | workflow, receiver status, pipeline checks |

Read the actual script and `--help` before use. Keep shared schemas compatible;
evidence/binding/request changes create new revisions. A single integration owner
coordinates live deployments. Source worktrees do not isolate ports/devices/data.
Live validation requires fresh process/session evidence, user consent and idle
deployment windows. Do not repeat old failed privilege routes without new evidence.
