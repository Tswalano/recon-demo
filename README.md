# Recon demo

A small app for trying **Workers & jobs** in the Ops Console. It reconciles a ledger against a bank statement and
shows, on one page, who ran each reconciliation:

- **the scheduled job**: runs on a schedule, then exits (`python -m app.jobs.recon`);
- **the worker**: always running, beats every 5 seconds and runs the recons queued from the page (`python -m app.worker`);
- **the button** on the page: runs one in the web process, for comparison.

The page shows whether the worker is alive (its last heartbeat), the queue, when the schedule last ran, every run
(matched / amounts that differ / missing on one side, how long it took) and, for a run you click, what it found.
It refreshes every 3 seconds.

FastAPI and SQLAlchemy. The queue lives in the app's own database, so no Redis is needed.

## Set it up with the Ops Console

### 1. Create the app (New app)
On the server's page → **New app**:

| Field | Value |
|---|---|
| Repository | this repository, branch `main` |
| Runtime | **Python** (detected: FastAPI, module `app.main:app`) |
| Python version | 3.12 (3.11 or newer) |
| Migrations | none (the app creates its tables on start) |
| Health path | `/health` |
| Database | **SQLite** is the simplest (nothing to install). **MySQL (MariaDB)** or **PostgreSQL** work too: New app suggests MySQL because the app depends on `pymysql`; choose what you want to try. |

Create it. When it's live, open its address: the page loads and says the worker is **not running** (nothing has added
it yet) and that no scheduled run has happened.

### 2. Add the worker
App page → **Workers & jobs** → **Add**:

| Field | Value |
|---|---|
| Name | `worker` |
| Type | Worker (always running) |
| Command | `python -m app.worker` |

**Add and start.** Within a few seconds the page shows the worker **Running** with a fresh heartbeat. Click
**Queue a recon for the worker**: it picks it up within 5 seconds and a run tagged **worker** appears.

### 3. Add the scheduled job
**Add** again:

| Field | Value |
|---|---|
| Name | `recon` |
| Type | Scheduled job |
| Command | `python -m app.jobs.recon` |
| Runs | **Every 15 minutes** to watch it quickly, or *Daily at 02:00 (SAST)* for a nightly recon |

**Add and schedule**, then **Run now** to see a run tagged **schedule** straight away. The Workers & jobs tab shows
the job's last result and next run; the page shows "Last scheduled run".

### 4. Watch it in the Ops Console
On the app's **Workers & jobs** tab (it refreshes every 5 seconds):
- the worker's row shows its progress while it works, e.g. **Running · 20/30**;
- click a name to open it: what it reports (**Recon task #7**, a progress bar *processed / total*, tasks done / failed,
  the queue) and its run history (for the job: when each run started, how long it took, succeeded or failed).

The app reports through one convention any worker or job can use: print a line starting with `ops-metric ` and a JSON
object, e.g. `ops-metric {"label": "Recon task #7", "processed": 20, "total": 30, "tasks_done": 4}`. The console shows
the latest one: `processed` / `total` as a progress bar, `label` as its title, any other numbers or short texts as
figures. See `metric()` in `app/recon.py`.

### 5. Things to try
- **Stop and start the server**: the worker comes back with it, and a missed nightly run happens when it starts.
- **Deploy** a change: the worker is restarted with the new code (the job uses it on its next run).
- **Remove the worker** in Workers & jobs: the page shows it as not running after 20 seconds, and queued recons wait.
- **Logs**: each process logs one line per recon (`recon #12 (worker): 89 matched, 5 amounts differ, 6 missing`).

## Run it locally

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000        # the page: http://localhost:8000
python -m app.worker                              # in a second terminal
python -m app.jobs.recon                          # a scheduled run, by hand
```

Without `DATABASE_URL` it uses `./recon.db` (SQLite). With the Ops Console, `DATABASE_URL` comes from the app's
settings (`sqlite:///…`, `mysql+pymysql://…` or `postgresql://…`).

## What's where

| Path | What |
|---|---|
| `app/main.py` | the web app: the page, `/api/status`, `/api/queue`, `/api/sample`, `/api/run-now`, `/health` |
| `app/worker.py` | the worker: heartbeat and the queue |
| `app/jobs/recon.py` | the scheduled job |
| `app/recon.py` | the reconciliation and sample payments |
| `app/db.py` | the tables (payments, queue, runs, problems, heartbeat) |
| `app/static/index.html` | the page |
