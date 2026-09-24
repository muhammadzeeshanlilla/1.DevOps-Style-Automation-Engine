# DevOps Automation Engine Frontend

React 19 + JavaScript + CSS + Vite dashboard for the local FastAPI automation-engine backend.

## Run locally

Start the backend first:

```powershell
cd ..\backend
.\.venv\Scripts\python.exe -m uvicorn api.app:app --host 127.0.0.1 --port 8000
```

In another PowerShell terminal, start the frontend:

```powershell
cd ..\frontend
npm install
npm run dev
```

Open `http://localhost:5173`. The backend must remain at `http://127.0.0.1:8000` because that is the API's configured local origin contract.

For a different local API URL, set `VITE_API_BASE_URL` in your shell before starting Vite. Do not put credentials or private filesystem paths in this value or commit secrets in an environment file.

## API mapping

| UI area | Backend source |
| --- | --- |
| Header API indicator | `GET /api/health` and request availability |
| Engine Status | `GET /api/status` |
| Start Engine | `POST /api/engine/start` |
| Stop Engine | `POST /api/engine/stop` |
| Refresh Status | `GET /api/health`, `GET /api/status`, and `GET /api/tasks` |
| System Health | API health plus scheduler/monitor data from `GET /api/status` |
| Task Overview | Live counts/queue/current task from `GET /api/status`; configuration counts fall back to `GET /api/tasks` while stopped |
| Recent Execution | Current and last result fields from `GET /api/status` |
| Tasks page | `GET /api/tasks` |
| Activity Logs page | `GET /api/logs?limit=20` by default; selector supports 20, 50, or 100 |
| Settings page | Local frontend/API URLs and the tasks response's configuration source |

Missing runtime fields are displayed as None or Unavailable; the frontend does not invent execution history, task names, scheduler times, paths, or log details.

## Phase 1 scope

- Responsive dashboard with Dashboard, Tasks, Activity Logs, and Settings views
- Automatic status polling every four seconds with overlap protection
- Safe start/stop controls with loading and 200/202/409/503 handling
- Read-only task configuration
- Sanitized backend log summaries only
- API-offline and stale/missing runtime states
- No authentication, database, task editing, SMTP inputs, or frontend credential storage

The API client is centralized in `src/api/client.js`. React built-in state and hooks provide navigation and data handling; there is no routing or state-management dependency.

## Monitoring jobs - Phase 2

Open Tasks, choose **Create Monitoring Job**, enter and validate a local folder,
choose daily, minute, or hourly scheduling, and save. Start the engine from the
Dashboard. The Python backend - not the browser - monitors new, modified, and
deleted files and emails the accumulated report on schedule.

The Monitoring Jobs page uses the dedicated monitoring-job CRUD endpoints and
POST /api/folders/validate. The dashboard job counts come from
GET /api/monitoring-jobs. Settings reads the global sender, receiver, server, and
port from GET /api/email-settings.

Hourly choices are stored as equivalent minute intervals. Configuration controls
are disabled unless the engine is STOPPED and never restart it automatically.
Recipient email is shared, not per job. Per-event filtering, authentication, and
database storage are intentionally deferred.

## Secure local email setup

Settings provides sender, receiver, SMTP server, SMTP port, and a write-only
Google App Password field. Never enter a normal Gmail password. Enable Google
2-Step Verification, open https://myaccount.google.com/apppasswords, create an
App Password, and use the generated value here. Never share it or commit it.

With **Remember App Password securely on this device** enabled, the backend uses
the operating system credential store. Otherwise it keeps the credential only
for the current backend session. The password field clears after saving and is
never refilled, returned by the API, logged, or placed in browser storage.

Use **Send Test Email** only after saving. Success means SMTP accepted the
message; check the receiver inbox or spam folder. **Forget Saved App Password**
deletes OS-stored and session copies without changing the non-secret settings.
Email controls require the engine to be STOPPED.

## Folder selection

The Monitoring Job form provides **Browse Folder** beside the manual path field.
It asks the local FastAPI backend to open the operating-system directory picker,
fills the selected absolute path, and applies the existing validation. Cancelling
leaves the current path unchanged. If native selection is unavailable, the form
shows a safe fallback message and manual path entry plus **Validate Folder**
continue to work.

## Verify

```powershell
npm run lint
npm run build
```

Current verification:

- ESLint: passed with zero warnings/errors
- Vite production build: passed
- Browser flow: all four views, API connection, STOPPED, real start/RUNNING data, duplicate-start 409, cooperative stop, final STOPPED, and API-offline handling passed
- Responsive evidence: 1440 x 1024 and 390 x 844
- Browser console: zero errors in the final clean run

See `design-qa.md` and `qa/` for the final visual comparison and browser-rendered evidence.
