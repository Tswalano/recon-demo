# The demo, step by step (about 10 minutes)

Have three windows open: the app's page, the Ops Console's **Workers & jobs** tab for the app, and a terminal.
Each step works by SFTP (`sftp bankdrop@<server>`, then `put <file>`) or by **Upload** / drag-and-drop on the page.

| # | Do | What to point out |
|---|---|---|
| 1 | Open the page and the Ops Console. | The worker is **Running**, beating every few seconds, "Waiting for files". Nothing has arrived yet. |
| 2 | `put ledger_2026-11-26.csv` | It appears under **Files**, imported in a second or two. Click it: the payments it brought in. Only one side of the 26th is in, so nothing is reconciled yet. |
| 3 | `put bank_2026-11-26.csv` | Both sides are in, so the worker reconciles the day by itself: a **worker** run appears. Click it: the two files it used, the amounts that differ and the payments missing on one side. |
| 4 | `put ledger_2026-11-27.csv` and `put bank_2026-11-27_with-errors.csv` | The bank file is imported with **5 lines rejected**. Click it to see each line and why: a letter O in an amount, the wrong date, a repeated reference, no reference, a column short. The rest are imported and the 27th is reconciled. |
| 5 | `put bank_2026-11-26.csv` again | **Duplicate**: same content as a file already imported, so nothing is imported twice. |
| 6 | `put bank_2026-11-28_corrupt.csv`, `bank_2026-11-28_wrong-columns.csv`, `statement.csv` | Each **fails** with a plain reason (not a text file, the header is wrong, the name doesn't say the side and day) and is kept in `failed/`. The worker carries on. |
| 7 | `put ledger_2026-11-25_big.csv` (4,878 lines), and while the Ops Console shows **Importing… 1500/4878**, **Stop** the worker (or deploy). | The page shows the file **interrupted** and back in the inbox. Nothing half-imported was saved. **Start** the worker: it takes the file again and finishes. This is what a worker gives you over a request handler. |
| 8 | Ops Console → the `recon` job → **Run now** | A **schedule** run for 25 Nov: everything is missing at the bank, with the note **"No bank file received for 2026-11-25"**. The job reconciles whatever the worker didn't, and catches the file that never came. |
| 9 | (optional) Deploy a change, e.g. a new rule in `app/files.py`, then click **Drop sample files** on the page. | The worker restarts on the new code; the next files use it. |

To start again from nothing: stop the worker, delete the app's database (or its tables) and empty the folders in
`FILES_DIR`, then start it.

The files are made by `python -m app.samples samples/` (a fixed seed: the same files every time).
