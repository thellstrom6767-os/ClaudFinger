"""Storage and retrieval of supporting documents (underlag) for vouchers.

Documents are stored as BLOBs in the _ledger.db SQLite database.
The _underlag/ directory is written only during export (store.export_sie or
export_underlag below).  File naming uses the Verifikation convention:

  single file  → Verifikation_A5.pdf
  multi-file   → Verifikation_A5[1av2].pdf, Verifikation_A5[2av2].pdf
"""
from __future__ import annotations
import hashlib
import os
import sqlite3
import zlib
from datetime import date
from pathlib import Path

from .store import db_path as _db_path


def _connect(ledger_path: str) -> sqlite3.Connection:
    return sqlite3.connect(_db_path(ledger_path))


def _stored_filename(series: str, number: int, seq: int, total: int, ext: str) -> str:
    ref = f'{series}{number}'
    if total == 1:
        return f'Verifikation_{ref}{ext}'
    return f'Verifikation_{ref}[{seq}av{total}]{ext}'


def add_file(ledger_path: str, series: str, number: int, src_path: str) -> str:
    """Read src_path and store its contents as a BLOB. Returns the derived stored filename."""
    original_name = os.path.basename(src_path)
    with open(src_path, 'rb') as f:
        raw = f.read()

    digest = hashlib.sha256(raw).hexdigest()
    blob = zlib.compress(raw)
    conn = _connect(ledger_path)
    try:
        conn.execute(
            'INSERT INTO underlag (series, number, original_name, added_at, data, sha256, compressed) '
            'VALUES (?,?,?,?,?,?,1)',
            (series, number, original_name, date.today().isoformat(), blob, digest),
        )
        conn.commit()
        total = conn.execute(
            'SELECT COUNT(*) FROM underlag WHERE series=? AND number=?',
            (series, number)
        ).fetchone()[0]
    finally:
        conn.close()

    ext = Path(original_name).suffix.lower()
    return _stored_filename(series, number, total, total, ext)


def list_for_voucher(ledger_path: str, series: str, number: int) -> list[dict]:
    conn = _connect(ledger_path)
    try:
        rows = conn.execute(
            'SELECT id, original_name, added_at FROM underlag '
            'WHERE series=? AND number=? ORDER BY id',
            (series, number)
        ).fetchall()
    finally:
        conn.close()
    total = len(rows)
    result = []
    for seq, (row_id, original_name, added_at) in enumerate(rows, start=1):
        ext = Path(original_name).suffix.lower()
        filename = _stored_filename(series, number, seq, total, ext)
        result.append({'id': row_id, 'filename': filename,
                       'original_name': original_name, 'added_at': added_at})
    return result


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
    """Remove a file by DB id. Returns original_name or None if not found."""
    conn = _connect(ledger_path)
    try:
        row = conn.execute(
            'SELECT original_name FROM underlag WHERE id=?', (file_id,)
        ).fetchone()
        if not row:
            return None
        conn.execute('DELETE FROM underlag WHERE id=?', (file_id,))
        conn.commit()
        return row[0]
    finally:
        conn.close()


def remove_all_for_voucher(ledger_path: str, series: str, number: int) -> int:
    """Remove all underlag for a voucher. Returns the number of rows deleted."""
    conn = _connect(ledger_path)
    try:
        result = conn.execute(
            'DELETE FROM underlag WHERE series=? AND number=?', (series, number)
        )
        conn.commit()
        return result.rowcount
    finally:
        conn.close()


def renumber_vouchers(
    ledger_path: str,
    renumber_map: dict[tuple[str, int], tuple[str, int]],
) -> int:
    """Update underlag rows to reflect new voucher numbers after a sort or delete.

    A plain UPDATE is safe because underlag rows carry no unique constraint on
    (series, number); multiple rows can share the same voucher reference.
    Returns the number of rows updated.
    """
    if not renumber_map:
        return 0
    conn = _connect(ledger_path)
    try:
        count = 0
        for (old_series, old_number), (new_series, new_number) in renumber_map.items():
            result = conn.execute(
                'UPDATE underlag SET series=?, number=? WHERE series=? AND number=?',
                (new_series, new_number, old_series, old_number),
            )
            count += result.rowcount
        conn.commit()
        return count
    finally:
        conn.close()


def get_data(ledger_path: str, file_id: int) -> bytes | None:
    """Return the decompressed data for a file, or None if not found."""
    conn = _connect(ledger_path)
    try:
        row = conn.execute(
            'SELECT data, compressed FROM underlag WHERE id=?', (file_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    data, compressed = row
    return zlib.decompress(data) if compressed else data


def open_file(ledger_path: str, file_id: int) -> str | None:
    """Write a BLOB to /tmp/bokforing_underlag/ and return the path.

    The temp file persists until the OS cleans /tmp; xdg-open can read it
    asynchronously after this function returns.
    """
    conn = _connect(ledger_path)
    try:
        row = conn.execute(
            'SELECT original_name, data, compressed FROM underlag WHERE id=?', (file_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    original_name, data, compressed = row
    raw = zlib.decompress(data) if compressed else data
    tmp_dir = '/tmp/bokforing_underlag'
    os.makedirs(tmp_dir, exist_ok=True)
    tmp_path = os.path.join(tmp_dir, f'{file_id}_{original_name}')
    with open(tmp_path, 'wb') as f:
        f.write(raw)
    return tmp_path


def export_underlag(ledger_path: str, underlag_dir: str) -> int:
    """Write all BLOB files to underlag_dir using the Verifikation naming convention.

    Returns the number of files written.
    """
    conn = _connect(ledger_path)
    try:
        rows = conn.execute(
            'SELECT series, number, original_name, data, sha256, compressed FROM underlag '
            'ORDER BY series, number, id'
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return 0

    os.makedirs(underlag_dir, exist_ok=True)

    from collections import defaultdict
    groups: dict[tuple[str, int], list] = defaultdict(list)
    for series, number, original_name, data, sha256, compressed in rows:
        groups[(series, number)].append((original_name, data, sha256, compressed))

    count = 0
    for (series, number), files in sorted(groups.items()):
        total = len(files)
        for seq, (original_name, data, stored_sha256, compressed) in enumerate(files, start=1):
            ext = Path(original_name).suffix.lower()
            filename = _stored_filename(series, number, seq, total, ext)
            raw = zlib.decompress(data) if compressed else data
            out_file = os.path.join(underlag_dir, filename)
            with open(out_file, 'wb') as f:
                f.write(raw)
            if stored_sha256 is not None:
                actual = hashlib.sha256(raw).hexdigest()
                if actual != stored_sha256:
                    raise RuntimeError(
                        f'SHA-256 mismatch for {filename}: '
                        f'stored={stored_sha256} actual={actual}'
                    )
            count += 1

    return count
