"""SIE 4 file parser and writer. Encoding: CP437 (PC8)."""
from __future__ import annotations
import os
import shlex
from datetime import date
from decimal import Decimal, InvalidOperation

from .models import Account, SIEFile, Transaction, Voucher

ENCODING = 'cp437'
PROGRAM_NAME = "Claude's converter"
PROGRAM_VERSION = '2026-05-20'


def _today() -> str:
    return date.today().strftime('%Y%m%d')


def _tokenize(line: str) -> list[str]:
    """Split a #TAG ... line into tokens, respecting quoted strings."""
    line = line.strip()
    if not line.startswith('#'):
        return []
    try:
        return shlex.split(line[1:])
    except ValueError:
        return line[1:].split()


def _dec(s: str) -> Decimal:
    try:
        return Decimal(s)
    except InvalidOperation:
        return Decimal('0')


def parse(path: str) -> SIEFile:
    """Parse a SIE 4 file and return a SIEFile object."""
    sie = SIEFile()
    account_map: dict[str, Account] = {}
    current_ver: Voucher | None = None
    in_ver = False

    with open(path, encoding=ENCODING, errors='replace') as f:
        for raw in f:
            line = raw.rstrip()
            stripped = line.strip()

            if stripped == '{':
                in_ver = True
                continue
            if stripped == '}':
                if current_ver is not None:
                    sie.vouchers.append(current_ver)
                    current_ver = None
                in_ver = False
                continue

            tokens = _tokenize(stripped)
            if not tokens:
                continue
            tag = tokens[0].upper()

            if in_ver:
                if tag == 'TRANS' and current_ver is not None and len(tokens) >= 4:
                    current_ver.transactions.append(Transaction(
                        account=tokens[1],
                        amount=_dec(tokens[3]),
                        date=tokens[4] if len(tokens) > 4 else current_ver.date,
                        label=tokens[5] if len(tokens) > 5 else '',
                    ))
                continue

            if tag == 'PROGRAM' and len(tokens) >= 3:
                sie.program, sie.program_version = tokens[1], tokens[2]
            elif tag == 'GEN' and len(tokens) >= 2:
                sie.gen_date = tokens[1]
                sie.gen_author = tokens[2] if len(tokens) > 2 else ''
            elif tag == 'ORGNR' and len(tokens) >= 2:
                sie.org_nr = tokens[1]
            elif tag == 'FNAMN' and len(tokens) >= 2:
                sie.company_name = tokens[1]
            elif tag == 'ADRESS' and len(tokens) >= 5:
                sie.contact, sie.street, sie.zip_city, sie.phone = (
                    tokens[1], tokens[2], tokens[3], tokens[4])
            elif tag == 'RAR' and len(tokens) >= 4 and tokens[1] == '0':
                sie.year_begins, sie.year_ends = tokens[2], tokens[3]
            elif tag == 'VALUTA' and len(tokens) >= 2:
                sie.currency = tokens[1]
            elif tag == 'KONTO' and len(tokens) >= 2:
                acc = Account(number=tokens[1], label=tokens[2] if len(tokens) > 2 else '')
                sie.accounts.append(acc)
                account_map[acc.number] = acc
            elif tag == 'KTYP' and len(tokens) >= 3 and tokens[1] in account_map:
                account_map[tokens[1]].ktyp = tokens[2]
            elif tag == 'SRU' and len(tokens) >= 3 and tokens[1] in account_map:
                account_map[tokens[1]].sru.append(tokens[2])
            elif tag == 'IB' and len(tokens) >= 4 and tokens[1] == '0':
                sie.ib[tokens[2]] = _dec(tokens[3])
            elif tag == 'UB' and len(tokens) >= 4 and tokens[1] == '0':
                sie.ub[tokens[2]] = _dec(tokens[3])
            elif tag == 'RES' and len(tokens) >= 4 and tokens[1] == '0':
                sie.res[tokens[2]] = _dec(tokens[3])
            elif tag == 'VER' and len(tokens) >= 4:
                current_ver = Voucher(
                    series=tokens[1],
                    number=int(tokens[2]) if tokens[2].isdigit() else 0,
                    date=tokens[3],
                    label=tokens[4] if len(tokens) > 4 else '',
                    reg_date=tokens[5] if len(tokens) > 5 else _today(),
                    signature=tokens[6] if len(tokens) > 6 else '',
                )

    return sie


def _fmt(amount: Decimal) -> str:
    return f'{amount:.2f}'


def _format_voucher(v: Voucher) -> str:
    lines = [
        f'#VER "{v.series}" {v.number} {v.date} "{v.label}" {v.reg_date} "{v.signature}" ',
        '{',
    ]
    for t in v.transactions:
        lines.append(f'#TRANS {t.account} {{}} {_fmt(t.amount)} {v.date} "{t.label}" 0.0')
    lines.append('}')
    return '\n'.join(lines)


def append_voucher(path: str, voucher: Voucher) -> None:
    """Append a voucher to an existing SIE file atomically."""
    tmp = path + '.tmp'
    with open(path, 'rb') as f:
        content = f.read()
    addition = ('\n' + _format_voucher(voucher) + '\n').encode(ENCODING, errors='replace')
    with open(tmp, 'wb') as f:
        f.write(content)
        f.write(addition)
    os.replace(tmp, path)


def write(path: str, sie: SIEFile) -> None:
    """Write a complete SIE 4 file."""
    today = _today()
    lines = [
        '#FLAGGA 0',
        f'#PROGRAM "{sie.program}" "{sie.program_version or PROGRAM_VERSION}"',
        '#FORMAT PC8',
        f'#GEN {sie.gen_date or today} "{sie.gen_author}"',
        '#SIETYP 4',
        f'#ORGNR {sie.org_nr}',
        f'#ADRESS "{sie.contact}" "{sie.street}" "{sie.zip_city}" "{sie.phone}"',
        f'#FNAMN "{sie.company_name}"',
        f'#RAR 0 {sie.year_begins} {sie.year_ends}',
        '#VALUTA SEK',
    ]
    for acc in sie.accounts:
        lines.append(f'#KONTO {acc.number} "{acc.label}"')
    for acc in sie.accounts:
        if acc.ktyp:
            lines.append(f'#KTYP {acc.number} {acc.ktyp}')
    for acc in sie.accounts:
        for sru in acc.sru:
            lines.append(f'#SRU {acc.number} {sru}')
    for acct, amount in sorted(sie.ib.items()):
        lines.append(f'#IB 0 {acct} {_fmt(amount)}')
    for acct, amount in sorted(sie.ub.items()):
        lines.append(f'#UB 0 {acct} {_fmt(amount)}')
    for acct, amount in sorted(sie.res.items()):
        lines.append(f'#RES 0 {acct} {_fmt(amount)}')
    for v in sie.vouchers:
        lines.append('')
        lines.append(_format_voucher(v))
    with open(path, 'w', encoding=ENCODING, errors='replace') as f:
        f.write('\n'.join(lines) + '\n')
