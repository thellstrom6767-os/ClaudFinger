# bokforing — Claude Code guidance

## Project overview

CLI accounting application for Retsina Consulting AB (Swedish AB).
Primary storage is SQLite (`*_ledger.db`). SIE 4 `.se` files are export-only
(written by `bokforing export`; original `.se` kept as backup after migration).
SIE 5 zip packages are used for archiving/exchange.

## Documentation rule

**Whenever you add, remove, or change a CLI command, or change the storage
format, you must update the relevant documentation before committing:**

- `Documentation/command_reference.rst` — command options, behaviour,
  examples, and workflow snippets.
- `Documentation/storage_format.rst` — file formats, SQLite schema,
  SIE 5 XML structure, year-transition behaviour, data integrity notes.

Both files must stay in sync with the code. Never commit a code change that
affects the CLI surface or the storage format without a corresponding doc
update in the same commit.

## Project structure

```
bokforing/
    __init__.py
    models.py       — dataclasses: SIEFile, Voucher, Transaction, Account
    store.py        — authoritative DB layer: open_ledger, save_ledger, export_sie, db_path
    sie.py          — SIE 4 parser and writer (CP437); used for migration/export only
    ledger.py       — balance computation, account lookup, year init
    reports.py      — Resultatrapport and Balansrapport ODS generators
    sie5.py         — SIE 5 export (generate_sie5) and import (restore_from_sie5)
    underlag.py     — binary document BLOB store (SQLite-backed via store.db_path)
    cli.py          — Click CLI: all commands
main.py             — entry point
Documentation/
    command_reference.rst
    storage_format.rst
requirements.txt    — click>=8.0, odfpy>=1.4
```

## Storage architecture

- `store.open_ledger(path)` is the single entry point for reading a ledger.
  On first call against a `.se` path it auto-migrates: parses `.se`, creates
  `_ledger.db`, ingests underlag BLOBs, deletes old `_underlag.db` and dir.
- `store.save_ledger(path, sie)` is the single entry point for writing.
  Uses a `BEGIN/COMMIT` SQLite transaction; underlag rows are untouched.
- `store.export_sie(path, out)` writes a `.se` file and a `_underlag/` dir.
- Underlag documents are stored as BLOBs in the `underlag` table.
  The `_underlag/` directory is only written during `export`.
- `store.db_path(p)` normalises any `.se` or `_ledger.db` path to `_ledger.db`.

## Encoding

SIE 4 files are always written and read with `encoding='cp437',
errors='replace'`. Never change this to UTF-8 — CP437 is mandated by
the SIE standard.

## Sign convention

SIE uses standard double-entry signs throughout:

- Asset accounts (1xxx): positive = debit = the asset has a value.
- Liability/equity (2xxx): negative = credit = the liability exists.
- Income (3xxx): negative = credit = income earned.
- Expense (4xxx–8xxx): positive = debit = cost incurred.

The `reports.py` generators **negate** P&L amounts for display (income →
positive, costs → negative). The `balansrapport` preserves SIE signs as-is.
Do not change either convention without updating both the code and the
storage_format.rst sign-convention sections.

## Key constraints

- `ledger.init_from_previous` must only carry forward accounts 1000–2999.
  Income/expense accounts (3000–8999) always reset to zero each year.
- `underlag.py` naming: single file → `Verifikation_{S}{n}.{ext}`, multiple
  → `Verifikation_{S}{n}[{i}av{total}].{ext}`. The filename is derived at
  query time from seq/total; there is no filename column in the DB.
- SIE 5 round-trip: SRU codes are not carried. Document this whenever
  sie5import is described or modified.
- arsredovisning tool uses `sie_module.parse()` directly on the `.se` file.
  Run `bokforing export` to get an up-to-date `.se` before generating
  årsredovisning.
