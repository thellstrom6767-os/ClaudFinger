#!/usr/bin/env python3
"""
Fetch authoritative data sources and write taxreturn/data/*.json.

Sources
-------
  Skatteverket ZIP  — field specs for INK2, INK2R, INK2S
      Page: https://www.skatteverket.se/foretag/inkomstdeklaration/
            forredovisningsbyraer/tekniskinformationomfiloverforing...
      The page lists ZIP downloads named
          _Nyheter_from_beskattningsperiod_YYYYPN.zip
      The most recent one is authoritative.

  bas.se Excel  — BAS account → INK2R SRU field mapping
      Page: https://www.bas.se/kontoplaner/sru/
      Look for INK2_P1-*.xlsx download link.

Usage
-----
  python taxreturn/scripts/fetch_data.py
  python taxreturn/scripts/fetch_data.py --skv-zip /path/skv.zip --bas-xlsx /path/bas.xlsx

Writes:
  taxreturn/data/ink2_fields.json
  taxreturn/data/ink2r_fields.json
  taxreturn/data/ink2s_fields.json
  taxreturn/data/bas_ink2r.json
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

import xlrd       # pip install xlrd   (for .xls)
import openpyxl   # pip install openpyxl (for .xlsx)

DATA_DIR = Path(__file__).parent.parent / 'data'

SKV_PAGE = (
    'https://www.skatteverket.se/foretag/inkomstdeklaration/'
    'forredovisningsbyraer/tekniskinformationomfiloverforing'
    '.4.13948c0e18e810bfa0cca8.html'
)
BAS_SRU_PAGE = 'https://www.bas.se/kontoplaner/sru/'


# ─── web helpers ─────────────────────────────────────────────────────────────

def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def _scrape_skv_zip_url(html: str) -> str:
    """Return the most recent Nyheter ZIP URL from the Skatteverket page."""
    hits = re.findall(
        r'(/download/[^\s"\']+_Nyheter_from_beskattningsperiod_(\d{4}P\d)\.zip)',
        html,
    )
    if not hits:
        raise RuntimeError('Could not find ZIP download link on Skatteverket page')
    # sort by period descending, pick latest
    hits.sort(key=lambda x: x[1], reverse=True)
    path = hits[0][0]
    return f'https://www.skatteverket.se{path}'


def _scrape_bas_xlsx_url(html: str) -> str:
    """Return the INK2_P1-*.xlsx URL from the bas.se SRU page."""
    hits = re.findall(r'https://[^\s"\']+INK2_P1-[^_\s"\']+\.xlsx', html)
    if not hits:
        raise RuntimeError('Could not find INK2_P1 xlsx link on bas.se SRU page')
    return hits[0]


# ─── XLS field-table parser (Skatteverket) ───────────────────────────────────

def _parse_skv_xls(xls_bytes: bytes) -> dict:
    """Parse an INK2/INK2R/INK2S field-spec XLS into {field_code: {attr, datatype, sign, rule}}."""
    wb = xlrd.open_workbook(file_contents=xls_bytes)
    sheet = wb.sheets()[0]
    result: dict = {}
    for rx in range(sheet.nrows):
        row = [str(sheet.cell_value(rx, c)).strip() for c in range(sheet.ncols)]
        attr, faltnamn, datatype, obl, sign, rule = (row + [''] * 6)[:6]
        # field code column is 'Fältnamn'; numeric entries are field codes
        if not re.match(r'^\d{2,5}$', faltnamn):
            continue
        result[faltnamn] = {
            'attr':     attr,
            'datatype': datatype,
            'sign':     sign.strip() or '*',
            'rule':     rule.strip(),
        }
    return result


# ─── BAS account pattern parser ──────────────────────────────────────────────

def _expand_pat(pat: str) -> tuple[int, int]:
    """'30xx' → (3000, 3099),  '30xx-37xx' → (3000, 3799),  '1088' → (1088, 1088)."""
    pat = pat.strip()
    if not pat:
        raise ValueError(f'empty pattern')
    if '-' in pat:
        lo_s, hi_s = pat.split('-', 1)
        lo = int(lo_s.replace('x', '0'))
        hi = int(hi_s.replace('x', '9'))
        return lo, hi
    return int(pat.replace('x', '0')), int(pat.replace('x', '9'))


def _parse_account_cell(raw) -> list[dict]:
    """
    Parse a BAS account cell like '30xx-37xx', '10xx (exkl. 1088)', etc.
    Returns list of {min, max, excl, netto}.
    """
    if raw is None:
        return []
    s = str(raw).strip()
    if not s:
        return []

    # Detect netto indicator
    netto: str | None = None
    if re.search(r'[Oo]m\s+netto\s*\+', s):
        netto = '+'
    elif re.search(r'[Oo]m\s+netto\s*-', s):
        netto = '-'

    # Strip leading sign indicators ('+ 899x', '– 899x')
    s = re.sub(r'^[\+\-–]\s*', '', s)

    # Extract and strip exclusion clause
    excl_nums: list[int] = []
    m = re.search(r'\(\s*exkl\.\s*([^)]+)\)', s)
    if m:
        for p in m.group(1).split(','):
            p = p.strip()
            if re.search(r'\d', p):
                try:
                    lo, hi = _expand_pat(p)
                    excl_nums.extend(range(lo, hi + 1))
                except ValueError:
                    pass
        s = s[:m.start()] + s[m.end():]

    # Strip netto + parenthetical noise
    s = re.sub(r'\([^)]*\)', '', s)

    results = []
    for part in s.split(','):
        part = part.strip()
        if not re.search(r'\d', part):
            continue
        try:
            lo, hi = _expand_pat(part)
        except (ValueError, AttributeError):
            continue
        excl_in = [e for e in excl_nums if lo <= e <= hi]
        results.append({'min': lo, 'max': hi, 'excl': excl_in, 'netto': netto})
    return results


def _parse_bas_xlsx(xlsx_bytes: bytes) -> dict:
    """Parse the bas.se INK2 coupling Excel into {sru_field: [{min,max,excl,netto}]}."""
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
    sheet = wb.active
    mapping: dict = {}
    for row in sheet.iter_rows(values_only=True):
        field_raw = row[0]
        account_raw = row[3] if len(row) > 3 else None
        if field_raw is None or not str(field_raw).strip().isdigit():
            continue
        field_code = str(int(float(str(field_raw))))
        ranges = _parse_account_cell(account_raw)
        if not ranges:
            continue
        if field_code not in mapping:
            mapping[field_code] = []
        mapping[field_code].extend(ranges)
    return mapping


# ─── main ────────────────────────────────────────────────────────────────────

def run(skv_zip_path: str | None, bas_xlsx_path: str | None) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # ── Skatteverket field specs ──────────────────────────────────────────────
    if skv_zip_path:
        print(f'Reading Skatteverket ZIP from {skv_zip_path}')
        with open(skv_zip_path, 'rb') as f:
            zip_bytes = f.read()
    else:
        print(f'Fetching Skatteverket page …')
        html = _fetch(SKV_PAGE).decode('utf-8', errors='replace')
        zip_url = _scrape_skv_zip_url(html)
        print(f'Downloading {zip_url} …')
        zip_bytes = _fetch(zip_url)

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        def _read_xls(prefix: str) -> bytes:
            matched = [n for n in names if n.startswith(prefix) and n.endswith('.xls')]
            if not matched:
                raise RuntimeError(f'No {prefix}*.xls in ZIP. Available: {names}')
            return zf.read(matched[0])

        ink2_data  = _parse_skv_xls(_read_xls('INK2_'))
        ink2r_data = _parse_skv_xls(_read_xls('INK2R_'))
        ink2s_data = _parse_skv_xls(_read_xls('INK2S_'))

    (DATA_DIR / 'ink2_fields.json').write_text(
        json.dumps(ink2_data,  ensure_ascii=False, indent=2), encoding='utf-8')
    (DATA_DIR / 'ink2r_fields.json').write_text(
        json.dumps(ink2r_data, ensure_ascii=False, indent=2), encoding='utf-8')
    (DATA_DIR / 'ink2s_fields.json').write_text(
        json.dumps(ink2s_data, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Wrote ink2_fields.json  ({len(ink2_data)} fields)')
    print(f'Wrote ink2r_fields.json ({len(ink2r_data)} fields)')
    print(f'Wrote ink2s_fields.json ({len(ink2s_data)} fields)')

    # ── bas.se BAS→INK2R mapping ──────────────────────────────────────────────
    if bas_xlsx_path:
        print(f'Reading bas.se Excel from {bas_xlsx_path}')
        with open(bas_xlsx_path, 'rb') as f:
            xlsx_bytes = f.read()
    else:
        print('Fetching bas.se SRU page …')
        html = _fetch(BAS_SRU_PAGE).decode('utf-8', errors='replace')
        xlsx_url = _scrape_bas_xlsx_url(html)
        print(f'Downloading {xlsx_url} …')
        xlsx_bytes = _fetch(xlsx_url)

    bas_data = _parse_bas_xlsx(xlsx_bytes)
    (DATA_DIR / 'bas_ink2r.json').write_text(
        json.dumps(bas_data, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Wrote bas_ink2r.json ({len(bas_data)} field mappings)')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--skv-zip',   metavar='FILE',
                    help='Local Skatteverket ZIP (skips web download)')
    ap.add_argument('--bas-xlsx',  metavar='FILE',
                    help='Local bas.se INK2_P1 Excel (skips web download)')
    args = ap.parse_args()
    try:
        run(args.skv_zip, args.bas_xlsx)
    except Exception as e:
        print(f'ERROR: {e}', file=sys.stderr)
        sys.exit(1)
