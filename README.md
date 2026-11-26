# Recon demo

A small app for trying **Workers & jobs** in the Ops Console. Each day the bank and our ledger send a statement file;
the app imports them and reconciles the two sides:

- **files arrive** by **SFTP** (the bank drops them on the server) or the page's **Upload** button, into one inbox;
- **the worker** (always running, `python -m app.worker`) imports each file as it lands, checks every line, sets bad
  lines aside with the reason, and once both files for a day are in, reconciles that day;
- **the scheduled job** (`python -m app.jobs.recon`) runs on a schedule, reconciles anything not yet reconciled, says
  which file never arrived, then exits;
- **the button** on the page runs a recon in the web process, for comparison.

The page shows whether the worker is alive (its heartbeat), the inbox, every file (imported / rejected lines / failed /
duplicate) and, for a file you click, **its records**: the payments it brought in and each line it couldn't import,
with why. Then every recon run (matched / amounts that differ / missing on one side) and, for a run you click, the
files it used and what it found. It refreshes every 3 seconds.

FastAPI and SQLAlchemy. The main queue lives in the app's own database, so no Redis is needed. For trying a Redis
queue (Valkey on the server, or an external Redis such as Upstash) there is a second, optional one: see
[Try it with Redis](#6-optional-try-it-with-redis-valkey-or-upstash).

## How a file goes through

```
 SFTP put / Upload ──► inbox/ ──(worker claims it: an atomic rename)──► processing/ ──► archive/<id>_<name>   imported
                                                                                     └► failed/<id>_<name>    failed / duplicate
```

- **Names** say what a file is: `bank_YYYY-MM-DD….csv` or `ledger_YYYY-MM-DD….csv` (e.g. `bank_2026-11-26.csv`).
- **Columns**: a header line, then `reference,date,amount,description` (description optional).
- **A line is rejected** (and the rest imported) when: the amount isn't a number or has more than 2 decimals, the date
  isn't the file's day, the reference is empty, too long, repeated in the file or already imported from another file,
  or a column is missing.
- **A file fails** when: its name doesn't say the side and day, it isn't UTF-8 text, the header lacks a column, it's
  over 20 MB (`MAX_FILE_MB`), or none of its lines could be imported. **A duplicate** (the same content as a file
  already imported) is set aside and nothing is imported twice.
- **All or nothing**: checking the lines writes nothing; the payments, rejected lines and result are saved in one
  transaction at the end. A worker stopped part-way (a deploy, Stop) puts the file back in the inbox; one that crashed
  or was killed has its file put back when it starts again. Either way it's imported again, from the start.
- **Files still arriving** aren't taken: hidden, `*.part`, `*.tmp` and `*.filepart` files are ignored, and a file is
  only taken once it hasn't changed for 2 seconds.
- **The recon** of a day runs when both its files are in (the worker queues it). Another file for the day (a
  correction) queues it again.

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
| Database | **SQLite** is the simplest (nothing to install). **MySQL (MariaDB)** or **PostgreSQL** work too. |
| Environment | `FILES_DIR=/srv/recon-files` (see step 2), and `APP_PASSWORD=…` to put a password on the page (any user name) |

`FILES_DIR` must be a folder **outside the app's code**, so a deploy doesn't replace it, and the same for the web app
and the worker. Without it, files go in `./files` next to the code. The page shows the folder it uses, under **Send files**.

### 2. Let the bank in by SFTP (on the server, once)
SSH to the EC2 server and run, as root:

```bash
sudo ./scripts/setup-sftp.sh --app-user <user the app runs as> --key bank.pub    # or --password
```

It creates `/srv/recon-files` with `inbox/ processing/ archive/ failed/`, and an SFTP-only user `bankdrop` that is
locked into that folder (no shell, no tunnels) and can only write into `inbox/`. The app's user owns the folders.
To find the app's user: `ps -o user= -p <the worker's pid, shown on the page>`. Allow port 22 from the bank's address
in the EC2 security group. `--help` lists the options; `--remove` takes it out again.

If the Ops Console runs apps in containers, `/srv/recon-files` must also be mounted into them at the same path.

Skip this step to demo with the **Upload** button only: it drops files into the same inbox.

### 3. Add the worker
App page → **Workers & jobs** → **Add**:

| Field | Value |
|---|---|
| Name | `worker` |
| Type | Worker (always running) |
| Command | `python -m app.worker` |

**Add and start.** Within a few seconds the page shows the worker **Running** with a fresh heartbeat.

### 4. Add the scheduled job
**Add** again:

| Field | Value |
|---|---|
| Name | `recon` |
| Type | Scheduled job |
| Command | `python -m app.jobs.recon` |
| Runs | **Every 15 minutes** to watch it quickly, or *Daily at 02:00 (SAST)* for a nightly recon |

### 5. Watch it in the Ops Console
On the app's **Workers & jobs** tab: the worker's row shows its progress while it works, e.g. **Importing
ledger_2026-11-25_big.csv · 1200/4878**, then the recon (**Recon task #3 (2026-11-26) · 200/400**). Click a name for
what it reports and its run history. The job's runs show as succeeded, or failed when a date couldn't be reconciled.

Any worker or job can report this way: print a line starting with `ops-metric ` and a JSON object, e.g.
`ops-metric {"label": "Importing bank.csv", "processed": 20, "total": 300, "rejected": 1}`. The console shows the latest
one: `processed` / `total` as a progress bar, `label` as its title, other numbers or short texts as figures. See
`metric()` in `app/recon.py`.

### 6. (Optional) Try it with Redis: Valkey or Upstash
A second queue, in Redis, next to the database one: the same recon, handed over through Redis instead.

1. On the **Workers & jobs** tab, under **Redis**, choose **Valkey on this server** or **External Redis** and paste the
   address (Upstash: the `rediss://default:…@….upstash.io:6379` one). The console saves it as `REDIS_URL` and restarts
   the app and its workers.
2. **Add** the Redis worker:

   | Field | Value |
   |---|---|
   | Name | `redis-worker` |
   | Type | Worker |
   | Command | `python -m app.redis_worker` |

3. **Add** the scheduled job that queues on Redis:

   | Field | Value |
   |---|---|
   | Name | `redis-recon` |
   | Type | Scheduled job |
   | Command | `python -m app.jobs.redis_recon` |
   | Runs | Custom (cron) `*/15 * * * *` |

4. On the page, the **Redis queue** card shows what's waiting, whether the Redis worker is up, how many it has done or
   failed, and which Redis (without its password). **Queue a recon on Redis** puts the newest day on it; the Redis
   worker picks it up straight away and its run shows in the runs list as **redis**.

How it works (`app/redisq.py`): the page and the job push onto the `recon-demo:queue` list; the worker takes the next
one and moves it to `recon-demo:processing` in the same command, so a worker stopped or killed part-way leaves it
there and puts it back on its next start. Results are counted (`recon-demo:count:done` / `:failed`) and the last 20
kept (`recon-demo:recent`). Things to try: stop the Redis worker, queue a few, start it again; set a wrong address
and see the worker say it can't reach Redis (and the Ops Console count it as a problem in its logs).

Upstash's free plan counts every command, so the worker waits on the queue with one blocking command of up to 20
seconds (`REDIS_BLOCK_SECONDS`) instead of checking it every few seconds, beats about once a minute, reports its
figures every 5 minutes while idle, and the page's Redis figures are cached for 5 seconds: about 7,000 commands a day
while idle, plus about 4 a refresh while the page is open (check your plan's limit). `REDIS_PREFIX` (default `recon-demo`) names the keys, if two apps
share one Redis.

## The demo

`samples/` holds the files for it, and [samples/README.md](samples/README.md) a 10-minute script: a clean pair, a file
with bad lines, a duplicate, broken files, a big file to stop the worker in the middle of, and a missing bank file
for the scheduled job to find.

## Run it locally

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000        # the page: http://localhost:8000
python -m app.worker                              # in a second terminal
python -m app.jobs.recon                          # a scheduled run, by hand
cp samples/ledger_2026-11-26.csv samples/bank_2026-11-26.csv files/inbox/    # as if they came by SFTP
```

Without `DATABASE_URL` it uses `./recon.db` (SQLite). With the Ops Console, `DATABASE_URL` comes from the app's
settings (`sqlite:///…`, `mysql+pymysql://…` or `postgresql://…`). `ROW_DELAY_MS` (default 3) slows each line a little
so progress can be watched; set it to 0 for full speed.

Tests: `pip install pytest httpx fakeredis && pytest`.

Upgrading from the version that generated random payments: the tables have new names, so nothing needs migrating.
The old tables (`transactions`, `tasks`, `recon_runs`, `recon_items`) are no longer used and can be dropped.

## What's where

| Path | What |
|---|---|
| `app/main.py` | the web app: the page, `/api/status`, `/api/files/{id}`, `/api/runs/{id}`, `/api/upload`, `/api/samples`, `/api/queue`, `/api/redis/queue`, `/api/run-now`, `/health` |
| `app/worker.py` | the worker: heartbeat, the inbox, the recon queue, recovery on start |
| `app/files.py` | the folders, taking a file from the inbox, importing and checking it |
| `app/recon.py` | the reconciliation of a day |
| `app/jobs/recon.py` | the scheduled job |
| `app/redisq.py`, `app/redis_worker.py`, `app/jobs/redis_recon.py` | the optional Redis queue, its worker and its scheduled job |
| `app/samples.py` | sample files (`python -m app.samples samples/` rewrites `samples/`) |
| `app/db.py` | the tables (files, rejected lines, payments, queue, runs, problems, heartbeat) |
| `app/static/index.html` | the page |
| `scripts/setup-sftp.sh` | the server's SFTP drop box |
| `tests/` | the whole path of a file, from the inbox to the recon; the Redis queue (with a fake Redis) |
