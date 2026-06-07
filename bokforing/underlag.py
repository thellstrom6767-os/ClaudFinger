"""Storage and retrieval of supporting documents (underlag) for vouchers.

Each ledger file 'ledger_2024.se' gets:
  - ledger_2024_underlag/   directory holding the actual files
  - ledger_2024_underlag.db SQLite index (series, number, filename, metadata)

Stored filenames follow the established convention:
  single file  → Verifikation_A5.pdf
  multi-file   → Verifikation_A5[1av2].pdf, Verifikation_A5[2av2].pdf
The DB is the source of truth; filenames are derived on write.
"""
from __future__ import annotations
import os
import shutil
import sqlite3
from datetime import date
from pathlib import Path


def _paths(ledger_path: str) -> tuple[str, str]:
    """Return (underlag_dir, db_path) derived from the ledger path."""
    base = os.path.splitext(os.path.abspath(ledger_path))[0]
    return base + '_underlag', base + '_underlag.db'


def _connect(ledger_path: str) -> sqlite3.Connection:
    _, db_path = _paths(ledger_path)
    conn = sqlite3.connect(db_path)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS underlag (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            series        TEXT    NOT NULL,
            number        INTEGER NOT NULL,
            filename      TEXT    NOT NULL,
            original_name TEXT    NOT NULL,
            added_at      TEXT    NOT NULL
        )
    ''')
    conn.commit()
    return conn


def _stored_filename(series: str, number: int, seq: int, total: int, ext: str) -> str:
    """Build a Verifikation filename following the established convention."""
    ref = f'{series}{number}'
    if total == 1:
        return f'Verifikation_{ref}{ext}'
    return f'Verifikation_{ref}[{seq}av{total}]{ext}'


def _rename_to_reflect_total(underlag_dir: str, conn: sqlite3.Connection,
                              series: str, number: int) -> None:
    """Rename existing files when total count changes (e.g. 1→2 files)."""
    rows = conn.execute(
        'SELECT id, filename, original_name FROM underlag '
        'WHERE series=? AND number=? ORDER BY id',
        (series, number)
    ).fetchall()
    total = len(rows)
    for seq, (row_id, old_name, original_name) in enumerate(rows, start=1):
        ext = Path(original_name).suffix.lower()  # always derived from original
        new_name = _stored_filename(series, number, seq, total, ext)
        if old_name != new_name:
            old_path = os.path.join(underlag_dir, old_name)
            new_path = os.path.join(underlag_dir, new_name)
            if os.path.exists(old_path):
                os.rename(old_path, new_path)
            conn.execute('UPDATE underlag SET filename=? WHERE id=?', (new_name, row_id))


def add_file(ledger_path: str, series: str, number: int, src_path: str) -> str:
    """Copy src_path into the underlag store and register it. Returns stored filename."""
    underlag_dir, _ = _paths(ledger_path)
    os.makedirs(underlag_dir, exist_ok=True)

    original_name = os.path.basename(src_path)
    ext = Path(original_name).suffix.lower()

    conn = _connect(ledger_path)
    try:
        # Insert with a placeholder filename first to get the new total
        conn.execute(
            'INSERT INTO underlag (series, number, filename, original_name, added_at) '
            'VALUES (?,?,?,?,?)',
            (series, number, '__tmp__', original_name, date.today().isoformat())
        )
        conn.commit()

        # Rename all files for this voucher to reflect the new total
        _rename_to_reflect_total(underlag_dir, conn, series, number)
        conn.commit()

        # Find out what filename was assigned to our new row
        new_row = conn.execute(
            'SELECT id, filename FROM underlag WHERE series=? AND number=? ORDER BY id DESC LIMIT 1',
            (series, number)
        ).fetchone()
        filename = new_row[1]
        dst = os.path.join(underlag_dir, filename)
        shutil.copy2(src_path, dst)
        return filename
    finally:
        conn.close()


def list_for_voucher(ledger_path: str, series: str, number: int) -> list[dict]:
    conn = _connect(ledger_path)
    try:
        rows = conn.execute(
            'SELECT id, filename, original_name, added_at FROM underlag '
            'WHERE series=? AND number=? ORDER BY id',
            (series, number)
        ).fetchall()
    finally:
        conn.close()
    return [{'id': r[0], 'filename': r[1], 'original_name': r[2], 'added_at': r[3]}
            for r in rows]


def list_all(ledger_path: str) -> list[dict]:
    conn = _connect(ledger_path)
    try:
        rows = conn.execute(
            'SELECT series, number, COUNT(*) FROM underlag '
            'GROUP BY series, number ORDER BY series, number'
        ).fetchall()
    finally:
        conn.close()
    return [{'series': r[0], 'number': r[1], 'count': r[2]} for r in rows]


def remove_file(ledger_path: str, file_id: int) -> str | None:
    """Remove a file by DB id. Returns the deleted filename or None if not found."""
    conn = _connect(ledger_path)
    try:
        row = conn.execute(
            'SELECT series, number, filename FROM underlag WHERE id=?', (file_id,)
        ).fetchone()
        if not row:
            return None
        series, number, filename = row
        underlag_dir, _ = _paths(ledger_path)
        filepath = os.path.join(underlag_dir, filename)
        if os.path.exists(filepath):
            os.unlink(filepath)
        conn.execute('DELETE FROM underlag WHERE id=?', (file_id,))
        conn.commit()
        # Rename remaining files for this voucher to keep numbering consistent
        _rename_to_reflect_total(underlag_dir, conn, series, number)
        conn.commit()
        return filename
    finally:
        conn.close()


def file_path(ledger_path: str, file_id: int) -> str | None:
    """Return the full path to a stored file, or None if not found."""
    conn = _connect(ledger_path)
    try:
        row = conn.execute(
            'SELECT filename FROM underlag WHERE id=?', (file_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    underlag_dir, _ = _paths(ledger_path)
    return os.path.join(underlag_dir, row[0])


def remove_all_for_voucher(ledger_path: str, series: str, number: int) -> int:
    """Remove all underlag files for a voucher. Returns the number of files removed."""
    underlag_dir, _ = _paths(ledger_path)
    conn = _connect(ledger_path)
    try:
        rows = conn.execute(
            'SELECT id, filename FROM underlag WHERE series=? AND number=? ORDER BY id',
            (series, number)
        ).fetchall()
        for _row_id, filename in rows:
            filepath = os.path.join(underlag_dir, filename)
            if os.path.exists(filepath):
                os.unlink(filepath)
        conn.execute('DELETE FROM underlag WHERE series=? AND number=?', (series, number))
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def renumber_vouchers(
    ledger_path: str,
    renumber_map: dict[tuple[str, int], tuple[str, int]],
) -> int:
    """Rename underlag files to reflect new voucher numbers after a sort.

    Uses a two-pass rename to avoid conflicts when old and new numbers
    overlap (e.g. old A:3 → A:5 while old A:5 → A:3):

      Pass 1 — rename every affected file to a collision-free temp name
               (``__sort_tmp_{db_id}.ext``).
      Pass 2 — rename from temp names to final Verifikation names,
               respecting the [1avN] multi-file convention.

    Returns the number of files renamed.
    """
    if not renumber_map:
        return 0

    underlag_dir, _ = _paths(ledger_path)
    conn = _connect(ledger_path)
    try:
        rows = conn.execute(
            'SELECT id, series, number, filename FROM underlag ORDER BY id'
        ).fetchall()

        affected = [
            (row_id, series, number, filename)
            for row_id, series, number, filename in rows
            if (series, number) in renumber_map
        ]
        if not affected:
            return 0

        # Pass 1: rename to temp names and update DB with new (series, number)
        for row_id, series, number, filename in affected:
            ext = Path(filename).suffix
            tmp_name = f'__sort_tmp_{row_id}{ext}'
            old_path = os.path.join(underlag_dir, filename)
            tmp_path = os.path.join(underlag_dir, tmp_name)
            if os.path.exists(old_path):
                os.rename(old_path, tmp_path)
            new_series, new_number = renumber_map[(series, number)]
            conn.execute(
                'UPDATE underlag SET filename=?, series=?, number=? WHERE id=?',
                (tmp_name, new_series, new_number, row_id),
            )
        conn.commit()

        # Pass 2: rename from temp names to final Verifikation names
        updated_vouchers = set(renumber_map.values())
        for new_series, new_number in sorted(updated_vouchers):
            _rename_to_reflect_total(underlag_dir, conn, new_series, new_number)
        conn.commit()

        return len(affected)
    finally:
        conn.close()
