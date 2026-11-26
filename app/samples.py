"""Sample statement files: what the bank and our ledger would send for a day.

    python -m app.samples samples/      # (re)writes the demo files in samples/ (see samples/README.md)

The page's "Drop sample files" button uses drop_pair() to put a fresh ledger + bank pair for today in the inbox.
"""
import csv
import io
import random
import string
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from .files import save_upload

PAYEES = ["Acme Supplies", "Blue Crane Logistics", "Cape Office Furniture", "Delta Fuel", "Eastgate Cleaning", "Fynbos Catering",
          "Garden Route Couriers", "Highveld Print", "Indaba Events", "Jacaranda IT", "Karoo Water", "Lowveld Security"]


def _csv(rows) -> str:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["reference", "date", "amount", "description"])
    w.writerows(rows)
    return out.getvalue()


def pair(day: date, n: int, rng: random.Random, prefix: str = "PAY") -> tuple[str, str]:
    """A day of payments as both sides see them: most agree; ~5% of amounts differ, ~3% are only in the ledger and
    ~3% only at the bank."""
    ledger, bank = [], []
    for _ in range(n):
        ref = f"{prefix}-" + "".join(rng.choices(string.ascii_uppercase + string.digits, k=8))
        amount = Decimal(rng.randint(1000, 250000)) / 100
        desc = rng.choice(PAYEES)
        roll = rng.random()   # < 0.03: only in the ledger; 0.03–0.06: only at the bank; else both
        if roll < 0.03 or roll >= 0.06:
            ledger.append([ref, day.isoformat(), f"{amount:.2f}", desc])
        if roll >= 0.03:
            at_bank = amount if roll < 0.06 or rng.random() > 0.05 else amount + Decimal(rng.choice([-50, -10, 5, 20]))
            bank.append([ref, day.isoformat(), f"{at_bank:.2f}", desc.upper()])
    return _csv(ledger), _csv(bank)


def drop_pair(n: int = 300) -> list[str]:
    """A fresh ledger and bank file for today, straight into the inbox (as if both had arrived by SFTP)."""
    today, stamp = date.today(), datetime.now().strftime("%H%M%S")
    ledger, bank = pair(today, n, random.Random(), prefix="S" + stamp)
    return [save_upload(f"ledger_{today}_sample-{stamp}.csv", [ledger.encode()]),
            save_upload(f"bank_{today}_sample-{stamp}.csv", [bank.encode()])]


def write_demo_set(folder: Path):
    """The files the demo script in samples/README.md uses (fixed seed: the same files every time)."""
    rng = random.Random(2026)
    folder.mkdir(parents=True, exist_ok=True)
    w = lambda name, text: (folder / name).write_text(text, encoding="utf-8")

    # 1. A clean pair: both sides for 26 Nov.
    ledger, bank = pair(date(2026, 11, 26), 400, rng, prefix="NOV26")
    w("ledger_2026-11-26.csv", ledger)
    w("bank_2026-11-26.csv", bank)

    # 2. A pair where the bank's file has bad lines in it.
    ledger, bank = pair(date(2026, 11, 27), 300, rng, prefix="NOV27")
    lines = bank.splitlines()
    lines[5] = lines[5].rsplit(",", 2)[0] + ",12O.50," + lines[5].rsplit(",", 1)[1]   # a letter O in the amount
    lines[12] = lines[12].replace("2026-11-27", "2026-11-28")                         # the wrong day
    lines[20] = lines[3]                                                              # a repeated line
    lines[33] = "," + lines[33].split(",", 1)[1]                                      # no reference
    lines[41] = lines[41].split(",")[0] + ",2026-11-27"                               # a column short
    w("ledger_2026-11-27.csv", ledger)
    w("bank_2026-11-27_with-errors.csv", "\n".join(lines) + "\n")

    # 3. A big ledger file with no bank file: a progress bar to watch, and a recon that says the bank file is missing.
    ledger, _ = pair(date(2026, 11, 25), 5000, rng, prefix="NOV25")
    w("ledger_2026-11-25_big.csv", ledger)

    # 4. Files that cannot be imported.
    (folder / "bank_2026-11-28_corrupt.csv").write_bytes(bytes(rng.randrange(256) for _ in range(2048)))
    w("bank_2026-11-28_wrong-columns.csv", "ref;value;when\nX1;10.00;2026-11-28\n")
    w("statement.csv", "reference,date,amount\nABC,2026-11-28,10.00\n")


if __name__ == "__main__":
    write_demo_set(Path(sys.argv[1] if len(sys.argv) > 1 else "samples"))
