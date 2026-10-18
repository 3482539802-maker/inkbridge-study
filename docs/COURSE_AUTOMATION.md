# Course runtime configuration

`config/study-workflow.example.json` is deliberately inert: supervision and
recognition are disabled until you prepare your own source files, workbook
manifest and verified/pending binding. It is not a ready-to-run personal course.

Replace `C:/InkBridge` with your actual checkout; place the config in the ignored
`output/learning/runtime.json`. Start with prepared course paths, test configuration
using the current CLI `--help`/validation mode, and verify no existing production
instance before starting. Enable capture supervision only after source/binding,
receiver and device prerequisites have been verified.

Use `recognition.backend = current_chat` for the user-invoked review route;
optional offline formula models require separately installed environments/weights.
Keep `tutor.mode = review` and `external_content_authorized = false` by default.
Do not embed API keys in configs; an explicitly configured endpoint must read its
key from the named environment variable. Do not print keys in logs.

`start-study.ps1 -RegisterAtLogon` changes a local scheduled task and is optional;
use it only when the user requests login startup. It cannot keep a sleeping PC
running, authorize recording or autonomously wake the current chat.
