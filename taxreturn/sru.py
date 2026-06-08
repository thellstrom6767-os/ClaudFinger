"""Write INFO.SRU and BLANKETTER.SRU files."""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from bokforing.models import SIEFile

from .loader import TaxReturn

_DATA = Path(__file__).parent / 'data'


def _load_json(name: str) -> dict:
    return json.loads((_DATA / name).read_text(encoding='utf-8'))


def _now() -> tuple[str, str]:
    """Return (YYYYMMDD, HHMMSS) for the current moment."""
    n = datetime.now()
    return n.strftime('%Y%m%d'), n.strftime('%H%M%S')


def _fmt_value(value: Any, datatype: str) -> str:
    """Format a value according to its SRU datatype."""
    if datatype.startswith('Datum'):
        return str(value)  # already YYYYMMDD
    if datatype == 'Str_X' or datatype.startswith('Str'):
        return str(value)
    if datatype == 'Numeriskt_B':
        # Unsigned integer — value must be non-negative
        return str(abs(int(Decimal(str(value)).to_integral_value())))
    if datatype.startswith('Numeriskt'):
        # Signed integer
        v = int(Decimal(str(value)).to_integral_value())
        return str(v) if v >= 0 else str(v)
    return str(value)


def _write_block(
    lines: list[str],
    form: str,
    period: str,
    org_nr: str,
    fields: dict[str, Any],
    fields_meta: dict,
    date_s: str,
    time_s: str,
) -> None:
    lines.append(f'#BLANKETT {form}-{period}')
    lines.append(f'#IDENTITET {org_nr} {date_s} {time_s}')
    for fcode, value in fields.items():
        meta = fields_meta.get(fcode, {})
        datatype = meta.get('datatype', 'Numeriskt_A')
        lines.append(f'#UPPGIFT {fcode} {_fmt_value(value, datatype)}')
    lines.append('#BLANKETTSLUT')


def write_info_sru(
    sie: SIEFile,
    supplement: dict,
    out_path: str | Path,
) -> None:
    """Write INFO.SRU to out_path."""
    date_s, time_s = _now()
    program = supplement.get('program', 'ClaudFinger')
    version = supplement.get('version', '0.1')

    lines = [
        '#DATABESKRIVNING_START',
        '#PRODUKT SRU',
        f'#SKAPAD {date_s} {time_s}',
        f'#PROGRAM {program} {version}',
        '#FILNAMN BLANKETTER.SRU',
        '#DATABESKRIVNING_SLUT',
        '#MEDIELEV_START',
        f'#ORGNR {sie.org_nr}',
        f'#NAMN {sie.company_name}',
    ]
    if sie.street:
        lines.append(f'#ADRESS {sie.street}')
    if sie.zip_city:
        parts = sie.zip_city.split(None, 1)
        if len(parts) == 2:
            lines.append(f'#POSTNR {parts[0]}')
            lines.append(f'#POSTORT {parts[1]}')
        else:
            lines.append(f'#POSTORT {sie.zip_city}')
    if sie.phone:
        lines.append(f'#TELEFON {sie.phone}')
    if sie.contact:
        lines.append(f'#KONTAKT {sie.contact}')
    lines.append('#MEDIELEV_SLUT')

    Path(out_path).write_text('\n'.join(lines) + '\n', encoding='utf-8')


def write_blanketter_sru(
    tr: TaxReturn,
    out_path: str | Path,
) -> None:
    """Write BLANKETTER.SRU containing INK2, INK2R, and INK2S blocks."""
    ink2_meta  = _load_json('ink2_fields.json')
    ink2r_meta = _load_json('ink2r_fields.json')
    ink2s_meta = _load_json('ink2s_fields.json')

    date_s, time_s = _now()
    lines: list[str] = []

    _write_block(lines, 'INK2',  tr.period, tr.org_nr,
                 tr.ink2_fields,  ink2_meta,  date_s, time_s)
    _write_block(lines, 'INK2R', tr.period, tr.org_nr,
                 tr.ink2r_fields, ink2r_meta, date_s, time_s)
    _write_block(lines, 'INK2S', tr.period, tr.org_nr,
                 tr.ink2s_fields, ink2s_meta, date_s, time_s)

    lines.append('#FIL_SLUT')
    Path(out_path).write_text('\n'.join(lines) + '\n', encoding='utf-8')
