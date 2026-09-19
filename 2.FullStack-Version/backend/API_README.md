# Backend API - Secure Local Email Phase

This copied FullStack backend adds a local FastAPI interface to the completed CLI engine. The CLI/core implementation remains unchanged. Phase 2 adds validated monitoring-job configuration endpoints; there is no authentication or database.

## Monitoring jobs - Phase 2

The workflow is: select a folder -> choose a report schedule -> save the job ->
start the engine -> monitor continuously -> email the accumulated activity report.

Monitoring jobs map to the existing folder_report task type and reuse FileMonitor,
the scheduler, FolderReportHandler, and the SMTP sender.

| Method | Path | Purpose |
| --- | --- | --- |
| POST | /api/folders/validate | Check that a path is an accessible directory; never list contents |
| GET/POST | /api/monitoring-jobs | List or create jobs |
| GET/PUT/DELETE | /api/monitoring-jobs/{job_id} | Read, update, or delete one job |
| PATCH | /api/monitoring-jobs/{job_id}/enabled | Enable or disable a job |
| GET | /api/email-settings | Read safe global sender/receiver/server/port settings |

Daily schedules use a 24-hour hour and minute. Hourly input maps to every_minutes
by multiplying by 60. Folder reports include new, modified, and deleted changes;
per-event filtering is intentionally deferred because folder_report does not
support it.

Mutations require an unambiguously STOPPED engine and otherwise return 409.
The complete schema-v2 result is validated by the existing loader, written to a
flushed temporary file, and atomically replaces only config/settings.json.
Failures preserve the old file. Existing generic tasks, email, and notifications
are retained. Email is global; the App Password is never written to configuration.

## Secure local email setup

The Settings UI updates sender, receiver, SMTP hostname, and port in validated
JSON. The App Password is write-only. With **Remember** enabled, keyring stores
it under service DevOpsAutomationEngine and the normalized sender account.
Windows uses Credential Manager; macOS uses Keychain; Linux requires a supported
Secret Service/keyring backend. No secure backend means a safe failure, never a
plaintext fallback.

With **Remember** disabled, the App Password exists only in API-process memory
and is lost when that process exits. GET responses report credential status but
never return the secret. DELETE /api/email-settings/credential removes both the
session copy and saved OS credential while preserving non-secret settings.

PUT /api/email-settings and POST /api/email-settings/test require a stopped
engine. Test email reuses the existing SMTP sender and reports SMTP acceptance,
not guaranteed inbox delivery. Automated tests never use a real SMTP server.

At API engine start, the credential is placed only in a copied child environment
as SMTP_PASSWORD. The existing engine and direct CLI SMTP_PASSWORD workflow are
unchanged.

### How to create a Gmail App Password

1. Enable Google 2-Step Verification.
2. Open Google Account Security, then App passwords.
3. Create an App Password for this application.
4. Enter the generated App Password in the dashboard.
5. Never use your normal Gmail password and never commit the App Password to Git.

Open https://myaccount.google.com/apppasswords for setup. If App passwords is
unavailable, the account may not currently be eligible.

## Install

Run PowerShell from the `2.FullStack-Version/backend` directory. Python 3.10+ is required for the API dependencies; the standard-library CLI retains its existing Python requirement.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-api.txt
```

If `.venv` already exists, skip the first command. Activation is not required.

Runtime dependencies are only FastAPI (HTTP routing, schemas, CORS and OpenAPI) and Uvicorn (ASGI server). Their required transitive dependencies are installed by pip. HTTPX is used only for API tests:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-api-test.txt
```

Verified on Windows / Python 3.14.7 with FastAPI 0.141.1, Uvicorn 0.53.0 and HTTPX 0.28.1. Other supported Python versions and Unix were not tested. Starlette 1.6.0 currently emits an upstream HTTPX TestClient deprecation warning; it does not affect the passing tests or the production API.

## Start the API

```powershell
.\.venv\Scripts\python.exe -m uvicorn api.app:app --host 127.0.0.1 --port 8000
```

Use one server process. If port 8000 is occupied, choose another port (for example 8001) and change the URLs below accordingly. Do not bind to `0.0.0.0` or publish this unauthenticated API to a network. Starting the API does not start the automation engine.

Open:

- http://127.0.0.1:8000/docs - interactive API documentation; POST controls are available here
- http://127.0.0.1:8000/api/health
- http://127.0.0.1:8000/api/status
- http://127.0.0.1:8000/api/tasks
- http://127.0.0.1:8000/api/logs?limit=20
- http://127.0.0.1:8000/openapi.json

Ctrl+C stops the API server, not an independently running engine. Stop the engine with the API stop endpoint or the existing CLI command.

## Endpoint contract

| Method | Path | Behavior |
| --- | --- | --- |
| GET | /api/health | API liveness only; not engine readiness |
| GET | /api/status | Core ownership state, PID, and safe fresh runtime observations |
| POST | /api/engine/start | Launch the existing CLI start command; 202 accepted, 409 duplicate/unsafe ownership, 503 launch/configuration failure |
| POST | /api/engine/stop | Existing instance-scoped cooperative stop; 200 ownership released, 202 still waiting, 409 unsafe ownership |
| GET | /api/tasks | Read-only validated tasks from the configuration on disk |
| GET | /api/logs | Sanitized recent log summaries; default limit 100, allowed range 1-200 |
| GET / PUT | /api/email-settings | Read safe status or save non-secret settings and a write-only credential |
| POST | /api/email-settings/test | Send an explicitly requested SMTP test message while stopped |
| DELETE | /api/email-settings/credential | Remove session and OS-stored credential copies |

Invalid request parameters return a generic 422 response without echoing supplied values. Operational failures use fixed safe messages rather than raw exceptions.

Start returns STARTING/RUNNING/STOPPING only after the launched CLI process owns the runtime. START_REQUESTED means launch was requested but ownership was not confirmed within the bounded wait; poll status rather than repeatedly starting. A 202 is not a guarantee that every worker is healthy. Concurrent requests still compete for the existing OS-level engine lock.

Stop waits up to five seconds. REQUESTED means the stop file was written, not that workers have finished. Poll status until STOPPED. Slow SMTP or workers may need longer; the API never force-kills a process or releases its lock.

Tasks contain ID, type, enabled state and safe trigger settings. Names, email subjects/bodies, SMTP settings and watched paths are intentionally omitted. The existing loader normalizes legacy configuration to the version-2 task model. The response identifies its source as configuration_on_disk and changes_require_restart=true: it does not pretend disk edits changed a running engine. No configuration edits are provided by the API.

Status reuses the core snapshot validator, matching instance ownership, PID and freshness. Missing, corrupt, stale or foreign snapshots are unavailable rather than trusted. ProcessManager's raw details and instance token are not exposed.

Logs read at most the last 64 KiB, skip partial records and traceback continuations, and return the most recent allowed records in chronological order. Messages are fixed allowlisted summaries, not raw log text. Unknown details are withheld. Recipient addresses, subjects, bodies, file names/paths and arbitrary error contents are not returned. The existing on-disk log is not changed and may contain private information; do not publish it. SMTP acceptance is never presented as verified inbox delivery.

## Engine reuse and CLI compatibility

```text
POST start -> subprocess: existing main.py start
           -> existing run_cli / Engine
           -> existing ProcessManager lock
           -> existing FileMonitor / JobScheduler / TaskRunner / TaskRegistry

POST stop  -> existing ProcessManager.request_stop
           -> instance-specific stop file
           -> existing Engine cooperative cleanup and ownership release

GET status -> existing ProcessManager.inspect_status
           -> existing read_runtime_snapshot + ownership recheck
GET tasks  -> existing configuration loader
GET logs   -> bounded privacy-safe view of existing engine.log
```

The API contains no scheduling, task execution, SMTP sending, or replacement engine. On Windows it launches the current Python installation's underlying interpreter directly, avoiding the virtual-environment redirector PID mismatch. The CLI is standard-library-only and does not need the API environment.

The existing commands still work from this backend:

```powershell
python main.py start
python main.py status
python main.py stop
```

As before, CLI start runs in the foreground; use another terminal for status/stop. CLI and API use the same derived project-root paths and runtime ownership. No machine-specific path is embedded in API source.

## Direct CLI configuration and credentials

Direct CLI usage keeps the existing `config/settings.json` loader rules. Configure SMTP_PASSWORD only in the environment, never in a file:

```powershell
$env:SMTP_PASSWORD = [System.Net.NetworkCredential]::new("", (Read-Host "SMTP password" -AsSecureString)).Password
```

The API-launched engine may retrieve a dashboard credential from session memory or the OS credential store and passes it only to the engine child. Starting the engine executes configured workflows and can send real SMTP messages; only start it when intended.

Task IDs are public operational identifiers: do not put secrets or sensitive customer data in them. The API also redacts the current SMTP_PASSWORD from returned IDs. All task parameters and raw error text are excluded.

CORS allows exactly `http://localhost:5173` for the future Vite frontend. Browser-origin checks also reject cross-site simple POST controls; same-origin Swagger and origin-less local CLI clients work. Only localhost/127.0.0.1 Host headers are accepted. These are local safeguards, not authentication. Frontend work is deferred.

## Test

```powershell
python -B -m unittest discover -s tests
.\.venv\Scripts\python.exe -B -m unittest discover -s api_tests
.\.venv\Scripts\python.exe -m pip check
```

Verification: **406 existing tests + 103 separate API tests = 509 passing tests**.

API tests cover safe schemas, ownership changes, corrupt/stale snapshots, secret exclusion, bounded logs, CORS/Host/origin safeguards, HTTP error semantics and start/stop behavior. Five integration tests run the real existing CLI/Engine with injected idle configuration and isolated temporary runtime directories. They verify CLI/API interoperability and concurrent ownership without sending SMTP or editing customer configuration.

Manual live-server checks cover health/status/tasks/logs, OpenAPI/docs, preflight CORS, private-field exclusion and invalid query handling. No real configured engine start or real SMTP delivery is included in these smoke checks.

## New package layout

```text
api/
  __init__.py
  app.py
  dependencies.py
  service.py
  routes/
    __init__.py
    health.py
    engine.py
    observations.py
  schemas/
    __init__.py
    responses.py
api_tests/
  __init__.py
  helpers.py
  test_http.py
  test_service.py
  test_integration.py
requirements-api.txt
requirements-api-test.txt
API_README.md
```

Existing README/DOCUMENTATION retain the CLI reference and historical milestones. Their new API pointers and the .venv ignore rule are the only changes outside the added API files.

Official references: [FastAPI CORS](https://fastapi.tiangolo.com/tutorial/cors/), [FastAPI testing](https://fastapi.tiangolo.com/tutorial/testing/), [Uvicorn settings](https://www.uvicorn.org/settings/).
