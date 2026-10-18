# Current-chat review

`course_tutor.py --request ... --output ... --backend current_chat` prepares
ink/region evidence; add `--learning-root ...` only when publishing through the
existing queue. Inspect each generated packet and its response schema before
transcribing. No cloud API key is required for the current-chat path.

Review all images directly, including text, formulas, diagrams and annotations.
Preserve uncertain/fragment states, contact completeness, hashes, source version
and preview/final distinctions. A reviewed candidate is not independent accuracy
ground truth or verified final edit state.

`recognition_chat.py --packet ... --response ... --output ...` validates the
response and writes a new artifact. Its optional `--learning-root ...` publishes
only a matching pending revision. Never attach an old review to newer writing or
resurrect superseded tasks. Tutoring feedback must cite valid evidence IDs and
pass the course workflow's inbox validation. Use not-assessed correctness when
final answer evidence is insufficient.

This workflow is user-invoked; it cannot autonomously wake the chat or guarantee
response latency. Optional endpoint/model paths require separate explicit setup.
