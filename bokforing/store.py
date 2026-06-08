"""Authoritative DB-backed storage for a ledger year.

The on-disk format is a single SQLite file, e.g. ledger_2024_ledger.db.
SIE 4 .se files are export-only artefacts written by export_sie(); they
are never read after the first open (which auto-migrates from .se to DB).
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from .models import Account, SIEFile, Transaction, Voucher


def db_path(ledger_path: str) -> str:
    """Normalise a .se path or _ledger.db path to the canonical _ledger.db path."""
    p = os.path.abspath(ledger_path)
    if p.endswith('_ledger.db'):
        return p
    base = os.path.splitext(p)[0]
    return base + '_ledger.db'


def _connect(db_p: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_p)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def _create_tables(conn: sqlite3.Connection) -> None:
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS accounts (
            number TEXT PRIMARY KEY,
            label  TEXT NOT NULL DEFAULT '',
            ktyp   TEXT,
            sru    TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS balances (
            type    TEXT NOT NULL,
            account TEXT NOT NULL,
            amount  TEXT NOT NULL,
            PRIMARY KEY (type, account)
        );
        CREATE TABLE IF NOT EXISTS vouchers (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            series    TEXT    NOT NULL,
            number    INTEGER NOT NULL,
            date      TEXT    NOT NULL DEFAULT '',
            label     TEXT    NOT NULL DEFAULT '',
            reg_date  TEXT    NOT NULL DEFAULT '',
            signature TEXT    NOT NULL DEFAULT '',
            UNIQUE(series, number)
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            voucher_id INTEGER NOT NULL REFERENCES vouchers(id),
            seq        INTEGER NOT NULL,
            account    TEXT    NOT NULL,
            amount     TEXT    NOT NULL,
            date       TEXT    NOT NULL DEFAULT '',
            label      TEXT    NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS underlag (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            series        TEXT NOT NULL,
            number        INTEGER NOT NULL,
            original_name TEXT NOT NULL,
            added_at      TEXT NOT NULL,
            data          BLOB NOT NULL
        );
    ''')
    conn.commit()


def _load_from_db(conn: sqlite3.Connection) -> SIEFile:
    meta = {r[0]: r[1] for r in conn.execute('SELECT key, value FROM meta').fetchall()}

    accounts = []
    for number, label, ktyp, sru_json in conn.execute(
            'SELECT number, label, ktyp, sru FROM accounts ORDER BY number').fetchall():
        sru = json.loads(sru_json) if sru_json else []
        accounts.append(Account(number=number, label=label, ktyp=ktyp, sru=sru))

    ib: dict[str, Decimal] = {}
    ub: dict[str, Decimal] = {}
    res: dict[str, Decimal] = {}
    for btype, account, amount in conn.execute(
            'SELECT type, account, amount FROM balances').fetchall():
        d = Decimal(amount)
        if btype == 'IB':
            ib[account] = d
        elif btype == 'UB':
            ub[account] = d
        elif btype == 'RES':
            res[account] = d

    vouchers = []
    for v_id, series, number, date, label, reg_date, signature in conn.execute(
            'SELECT id, series, number, date, label, reg_date, signature '
            'FROM vouchers ORDER BY series, number').fetchall():
        trans_rows = conn.execute(
            'SELECT account, amount, date, label FROM transactions '
            'WHERE voucher_id=? ORDER BY seq',
            (v_id,)
        ).fetchall()
        transactions = [
            Transaction(account=r[0], amount=Decimal(r[1]), date=r[2], label=r[3])
            for r in trans_rows
        ]
        vouchers.append(Voucher(
            series=series, number=number, date=date, label=label,
            reg_date=reg_date, signature=signature, transactions=transactions,
        ))

    return SIEFile(
        program=meta.get('program', "Claude's converter"),
        program_version=meta.get('program_version', ''),
        gen_date=meta.get('gen_date', ''),
        gen_author=meta.get('gen_author', ''),
        org_nr=meta.get('org_nr', ''),
        company_name=meta.get('company_name', ''),
        contact=meta.get('contact', ''),
        street=meta.get('street', ''),
        zip_city=meta.get('zip_city', ''),
        phone=meta.get('phone', ''),
        year_begins=meta.get('year_begins', ''),
        year_ends=meta.get('year_ends', ''),
        currency=meta.get('currency', 'SEK'),
        accounts=accounts,
        ib=ib, ub=ub, res=res,
        vouchers=vouchers,
    )


def _save_to_db(conn: sqlite3.Connection, sie: SIEFile) -> None:
    """Rewrite all ledger tables inside an open transaction. Does not touch underlag."""
    conn.execute('DELETE FROM transactions')
    conn.execute('DELETE FROM vouchers')
    conn.execute('DELETE FROM accounts')
    conn.execute('DELETE FROM balances')
    conn.execute('DELETE FROM meta')

    conn.executemany('INSERT INTO meta VALUES (?,?)', [
        ('program',         sie.program),
        ('program_version', sie.program_version),
        ('gen_date',        sie.gen_date),
        ('gen_author',      sie.gen_author),
        ('org_nr',          sie.org_nr),
        ('company_name',    sie.company_name),
        ('contact',         sie.contact),
        ('street',          sie.street),
        ('zip_city',        sie.zip_city),
        ('phone',           sie.phone),
        ('year_begins',     sie.year_begins),
        ('year_ends',       sie.year_ends),
        ('currency',        sie.currency),
    ])

    for acc in sie.accounts:
        conn.execute(
            'INSERT INTO accounts VALUES (?,?,?,?)',
            (acc.number, acc.label, acc.ktyp, json.dumps(acc.sru)),
        )

    for btype, bdict in [('IB', sie.ib), ('UB', sie.ub), ('RES', sie.res)]:
        for account, amount in bdict.items():
            conn.execute(
                'INSERT INTO balances VALUES (?,?,?)',
                (btype, account, str(amount)),
            )

    for v in sie.vouchers:
        cur = conn.execute(
            'INSERT INTO vouchers (series, number, date, label, reg_date, signature) '
            'VALUES (?,?,?,?,?,?)',
            (v.series, v.number, v.date, v.label, v.reg_date, v.signature),
        )
        v_id = cur.lastrowid
        for seq, t in enumerate(v.transactions):
            conn.execute(
                'INSERT INTO transactions (voucher_id, seq, account, amount, date, label) '
                'VALUES (?,?,?,?,?,?)',
                (v_id, seq, t.account, str(t.amount), t.date, t.label),
            )


def open_ledger(ledger_path: str) -> SIEFile:
    """Open a ledger DB, auto-migrating from a .se file on first call."""
    db_p = db_path(ledger_path)

    if not os.path.exists(db_p):
        _auto_migrate(ledger_path, db_p)

    conn = _connect(db_p)
    try:
        return _load_from_db(conn)
    finally:
        conn.close()


def _auto_migrate(ledger_path: str, db_p: str) -> None:
    """Create a _ledger.db from a legacy .se + _underlag.db pair."""
    p = os.path.abspath(ledger_path)
    if p.endswith('_ledger.db'):
        base = p[:-len('_ledger.db')]
        sie_path = base + '.se'
    else:
        sie_path = p

    if not os.path.exists(sie_path):
        raise FileNotFoundError(
            f'No ledger found: {db_p} does not exist and {sie_path} not found'
        )

    from .sie import parse as parse_sie
    sie = parse_sie(sie_path)

    conn = _connect(db_p)
    try:
        _create_tables(conn)
        with conn:
            _save_to_db(conn, sie)

        base_stem = os.path.splitext(sie_path)[0]
        old_db = base_stem + '_underlag.db'
        old_dir = base_stem + '_underlag'

        if os.path.exists(old_db):
            _migrate_underlag(conn, old_db, old_dir)
            import shutil
            os.unlink(old_db)
            if os.path.isdir(old_dir):
                shutil.rmtree(old_dir)
    finally:
        conn.close()


def _migrate_underlag(conn: sqlite3.Connection, old_db: str, old_dir: str) -> None:
    """Copy rows from the legacy _underlag.db into the new underlag BLOB table."""
    old_conn = sqlite3.connect(old_db)
    try:
        rows = old_conn.execute(
            'SELECT series, number, filename, original_name, added_at FROM underlag ORDER BY id'
        ).fetchall()
    finally:
        old_conn.close()

    for series, number, filename, original_name, added_at in rows:
        file_path_on_disk = os.path.join(old_dir, filename)
        if not os.path.exists(file_path_on_disk):
            continue
        with open(file_path_on_disk, 'rb') as f:
            data = f.read()
        conn.execute(
            'INSERT INTO underlag (series, number, original_name, added_at, data) '
            'VALUES (?,?,?,?,?)',
            (series, number, original_name, added_at, data),
        )
    conn.commit()


def save_ledger(ledger_path: str, sie: SIEFile) -> None:
    """Atomically persist a SIEFile to the DB. Underlag rows are not touched."""
    db_p = db_path(ledger_path)
    conn = _connect(db_p)
    try:
        _create_tables(conn)
        with conn:
            _save_to_db(conn, sie)
    finally:
        conn.close()


def export_sie(ledger_path: str, out_path: str) -> None:
    """Write a SIE 4 .se file and a _underlag/ directory from the DB.

    The .se file and directory are placed at out_path / out_path_stem_underlag/
    and can be given to legacy tools or archived alongside the .si5 export.
    """
    db_p = db_path(ledger_path)
    conn = _connect(db_p)
    try:
        sie = _load_from_db(conn)
        rows = conn.execute(
            'SELECT series, number, original_name, data FROM underlag '
            'ORDER BY series, number, id'
        ).fetchall()
    finally:
        conn.close()

    from .sie import write as sie_write
    sie_write(out_path, sie)

    if rows:
        base = os.path.splitext(os.path.abspath(out_path))[0]
        underlag_dir = base + '_underlag'
        os.makedirs(underlag_dir, exist_ok=True)
        groups: dict[tuple[str, int], list] = defaultdict(list)
        for series, number, original_name, data in rows:
            groups[(series, number)].append((original_name, data))
        for (series, number), files in sorted(groups.items()):
            total = len(files)
            for seq, (original_name, data) in enumerate(files, start=1):
                ext = Path(original_name).suffix.lower()
                if total == 1:
                    filename = f'Verifikation_{series}{number}{ext}'
                else:
                    filename = f'Verifikation_{series}{number}[{seq}av{total}]{ext}'
                with open(os.path.join(underlag_dir, filename), 'wb') as f:
                    f.write(data)
