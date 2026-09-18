# DevOps-Style Automation Engine

A CLI-only Python automation engine for configured email actions and folder-change reports. Define supported workflows in JSON without changing Python code.

## Completed features

- Start, cooperative stop and live runtime status.
- Multiple configured tasks with daily or every-X-minutes schedules.
- New, modified and deleted file detection, plus configured file-event execution.
- Timestamped task start/completion/failure/skipped and process logs.
- Configurable SMTP email actions and opt-in success/failure lifecycle notifications.
- Safe runtime observations and cooperative threaded shutdown with exclusive instance ownership.

All eight core internship requirements are implemented. Bonus functionality is not required for submission and has not been added.

## Requirements and setup

Python **3.9 or newer** is required. All dependencies are Python standard-library modules; no `pip install` is needed. Final verification ran on Windows with Python 3.14.7. Real Unix-host process behavior and every supported Python version have not been fully verified.

Clone/download the project, open its directory, then create local configuration:

```powershell
Copy-Item config/settings.tasks.example.json config/settings.json
New-Item -ItemType Directory -Force watched_folder
```

The copy command can overwrite existing local settings: back them up first if you already have configuration. Edit ordinary SMTP settings and desired tasks before starting. Local `config/settings.json` is ignored and should not be committed.

Use any configured SMTP provider compatible with this implementation: SMTP with **STARTTLS**, a trusted certificate matching the server hostname, and sender/password authentication. Gmail with `smtp.gmail.com:587` is one example, not a mandatory provider. Implicit TLS-only connections are not implemented.

## Configuration

`config/settings.example.json` is the legacy single-folder/daily-time template. Its `watch_folder` and `schedule` normalize into one enabled daily `folder_report` task. `config/settings.tasks.example.json` is the version-2 template for multiple tasks. Both are password-free and validated by the same loader.

A complete version-2 example:

```json
{
  "schema_version": 2,
  "email": {
    "sender": "sender@example.com",
    "receiver": "receiver@example.com",
    "smtp_server": "smtp.example.com",
    "smtp_port": 587
  },
  "tasks": [
    {
      "id": "daily-folder-report", "name": "Daily folder report",
      "type": "folder_report", "enabled": true,
      "trigger": {"type": "daily", "hour": 9, "minute": 0},
      "parameters": {"path": "watched_folder"}
    },
    {
      "id": "interval-email", "name": "Interval email",
      "type": "email", "enabled": false,
      "trigger": {"type": "interval", "every_minutes": 15},
      "parameters": {"subject": "Automation message", "body": "Configured email action."}
    },
    {
      "id": "file-event-email", "name": "File-event email",
      "type": "email", "enabled": false,
      "trigger": {"type": "file_event", "path": "watched_folder", "events": ["new", "modified", "deleted"]},
      "parameters": {"subject": "File change observed", "body": "Configured file-event action."}
    }
  ],
  "notifications": {"enabled": false, "notify_on_success": true, "notify_on_failure": true}
}
```

Replace placeholder SMTP values. Enable desired examples explicitly. `folder_report` supports daily/interval triggers; `email` supports daily/interval/file-event triggers. Email content remains the configured subject/body; event filenames are not automatically inserted. Disabled tasks produce SKIPPED once per scheduler start without invoking a handler.

Relative watched-folder paths resolve from the **project root**, not the terminal's current directory. Absolute paths remain absolute. Required folders must exist for enabled tasks. A report task and event tasks may share one monitor; competing enabled report consumers of the same folder are rejected. Configuration edits require engine restart.

### SMTP_PASSWORD

The password is read only from the `SMTP_PASSWORD` environment variable inherited by the starting process. Never put it in JSON, code, screenshots or Git. PowerShell can prompt without placing the password directly in command history:

```powershell
$smtpPasswordInput = Read-Host 'SMTP password' -AsSecureString
$env:SMTP_PASSWORD = [System.Net.NetworkCredential]::new('', $smtpPasswordInput).Password
Remove-Variable smtpPasswordInput
```

On Bash:

```bash
read -r -s -p "SMTP password: " SMTP_PASSWORD
printf '\n'
export SMTP_PASSWORD
```

For Gmail, use an eligible account's app password following the provider's account instructions. Other providers may use their own SMTP credentials. A new terminal needs its own environment setup. Missing/empty SMTP_PASSWORD fails clearly **before connecting**. The project does not automatically load `.env` files.

### Lifecycle notifications

Set `notifications.enabled` to true to attempt a separate completion/failure email, controlled by the two booleans. Omitting the section leaves notifications disabled; all three fields are required if supplied. Notifications use the shared email settings and recipient. SKIPPED tasks do not notify.

Action results and lifecycle-notification outcomes are separate: notification failure does not change the action result or recursively invoke tasks. `TaskExecutionResult.notification_accepted` describes the action's email/report acceptance, not the later lifecycle notification. SMTP acceptance never guarantees inbox delivery.

## CLI commands and status

```powershell
python main.py start
python main.py status
python main.py stop
```

Run `start` in one terminal and status/stop in another. Keep the starting terminal open: the engine is foreground while monitor/scheduler workers run in background threads. Commands also work from another directory using the absolute path to main.py.

An OS lock prevents duplicate owners. Runtime metadata contains an instance ID; stop requests target that instance, never an unrelated recorded PID. No PID signal is used. Stale modern metadata does not establish ownership. Legacy numeric PID files are preserved for manual migration.

Status separates ownership/lifecycle, loaded runtime observations and optional configuration currently on disk. Safe observations include task IDs/types/schedules, current action/notification phase, latest result, worker liveness and approximate queued observations. Bodies, names, paths and raw errors are excluded from runtime JSON. Missing, malformed, foreign or stale observations are unavailable rather than presented as live. Five-second freshness and roughly one-second publication make this a sampled view, not durable history. Status/stop work without valid workflow configuration.

Stop and Ctrl+C use the same cleanup: stop accepting queued dispatch work, signal all attempted components, then join workers. Ownership remains held in STOPPING until workers actually exit. Individual joins are bounded; total shutdown time is not guaranteed. Exit codes: 0 successful, 1 failed/uncertain or stop timeout, 2 invalid usage.

## Final architecture

```text
CLI start -> configuration loader -> WorkflowConfiguration -> Engine
  -> ProcessManager ownership -> required FileMonitor(s) + JobScheduler

FileMonitor(s) -> locked report list ------------------> FolderReportHandler
              -> EventDispatcher queue -> JobScheduler
Daily/interval deadlines -----------------> JobScheduler
  -> TaskRunner -> TaskRegistry -> EmailHandler / FolderReportHandler
  -> TaskExecutionResult -> optional LifecycleNotificationService

Engine + TaskRunner -> RuntimeStatus -> engine.status.json -> CLI status
All components -> logger -> logs/engine.log + console
CLI stop / Ctrl+C -> signal workers -> join -> owned cleanup -> release lock
```

Only enabled report/file-event definitions require monitors; the same watched directory shares one monitor. Event dispatch and report batches remain independent. Only FolderReportHandler collects and acknowledges/restores report batches. Unconfirmed acceptance restores once ahead of newer events, preserving legitimate duplicates. There is no second direct scheduler/report-email path.

Daily tasks use local time and claim the task ID/date before attempting it. Starting in the matching minute permits execution; starting afterward schedules tomorrow. Delayed work may run later that day. Intervals use monotonic time, wait a full interval initially and advance to the next future boundary after completion/failure without a catch-up burst.

## Project structure

```text
main.py
cli/handler.py
config/loader.py, settings.example.json, settings.tasks.example.json
scheduler/job_scheduler.py
tasks/models.py, registry.py, runner.py, email_action.py, report_task.py
tasks/file_monitor.py, events.py, event_dispatcher.py, email_task.py, notifications.py
utils/paths.py, logger.py, process_manager.py, runtime_status.py
tests/
docs/screenshots/
README.md, DOCUMENTATION.md, .gitignore
```

Local/runtime artifacts are settings.json, logs/, engine.lock, engine.pid, engine.status.json and engine.stop.<instance_id>; these are ignored.

## Logs and presentation evidence

Logs append timestamps, levels and messages to `logs/engine.log` and the console. Current source message formats include:

```text
Scheduler started with configured daily/interval tasks.
Task started | id='daily-folder-report' name='Daily folder report'
Task completed | id='daily-folder-report' name='Daily folder report'
Task failed | id='daily-folder-report' name='Daily folder report'
Task skipped | id='interval-email' name='Example interval email'
```

These are **message-format examples from source**, not a captured terminal session; completed and failed describe alternative outcomes. No real email was sent to generate them.

The images in docs/screenshots are **historical development evidence**, not captures of the final runtime. setting.png is a sanitized configuration illustration, with Gmail shown as an example. start.png, modified.png, log.png and status.png show earlier scheduler/control output; engine_start.png shows an earlier repository layout, including artifacts that should now be excluded. No new screenshots were generated during cleanup.

Manually replace these with current engine start, file detection, logs and running live status captures; add stopped-status and cooperative-stop captures. Keep credentials, email bodies and unnecessary private paths out of images. The [historical demo video](https://drive.google.com/file/d/1WjOk6bQcbCpkLlwu2riORXKQvXMvn2Vn/edit) also predates the final runtime and was not reverified during cleanup.

## Final verification

**406 tests passed; all 40 Python files passed AST syntax checks.** Both configuration examples validate. Tests use mocked SMTP, temporary resources and controlled workers/subprocesses; no real SMTP/network connection is used. Windows control and contention are exercised; Unix locking has mocked backend coverage. Intermediate totals in DOCUMENTATION are historical development milestones.

```powershell
python -B -m unittest discover -s tests -v
```

For a safe manual walkthrough, leave SMTP_PASSWORD unset in the starting terminal, configure a one-minute interval plus a daily time shortly ahead, and enable matching file-event tasks. Check stopped status, start, check live status, create/modify/delete a test file with at least six seconds between observations, inspect task attempts, stop, check stopped status and inspect logs. Email actions should fail before connecting in this mode. Successful acceptance is proven with mocks; real-provider testing requires separate explicit approval.

## Known limitations

- Sequential task execution: synchronous SMTP or filesystem work delays later tasks and shutdown. The ten-second SMTP timeout bounds socket waits, not the whole transaction or DNS resolution.
- Nonrecursive five-second polling can miss short-lived changes. Filesystem scan errors can return partial/empty snapshots and produce misleading deleted/new observations during recovery.
- Event/report state and daily claims are memory-only and unbounded. Process exit loses them; shutdown deliberately abandons queued file-event work. Whole-process restart can repeat a daily task in its matching minute.
- No retry system, durable task history, plugin discovery, parallel execution or exact next-run status.
- Ambiguous SMTP acknowledgements can cause duplicate later reports; acceptance is not inbox delivery.
- Thread liveness does not prove responsiveness. Unexpected worker failure has no automatic recovery.
- Real Unix-host process behavior is not fully verified. Prolonged Windows file contention can exceed bounded retries.
- Runtime logs include filenames and selected SMTP recipient/subject information. Redaction protects the known SMTP password in email/runner paths, not every possible arbitrary exception throughout the program.

These are disclosed operating limits and optional improvements, not missing core internship requirements. This folder remains CLI-only; any future UI version belongs in a separate copy.

## Pre-publish checklist

This workspace was not a Git repository during final review, so tracked files and history could not be verified. In the actual repository:

- Inspect `git status` and `git ls-files` before staging.
- Confirm local settings, .env files, logs, bytecode and runtime/temporary files are not tracked. Ignore rules do not remove existing tracked files or history.
- Inspect credential history privately; rotate any real credential ever exposed. Do not claim history is clean without checking it.
- Commit the final documentation and intended source/examples, then push the verified final version.
- Replace historical screenshots if presenting them as final-runtime evidence.
