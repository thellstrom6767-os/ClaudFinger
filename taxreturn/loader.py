"""Assemble all data needed for an INK2 tax return."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from bokforing.models import SIEFile
from bokforing.skatt import SkattBerakning, berakna_skatt

_Z = Decimal('0')
_DATA = Path(__file__).parent / 'data'


# ─── public types ─────────────────────────────────────────────────────────────

@dataclass
class TaxReturn:
    period: str                         # e.g. '2024P4'
    org_nr: str
    company_name: str
    year_begins: str                    # YYYYMMDD
    year_ends: str                      # YYYYMMDD
    ink2_fields:  dict[str, Any]        # {field_code: value}
    ink2r_fields: dict[str, Any]
    ink2s_fields: dict[str, Any]
    skatt: SkattBerakning
    warnings: list[str] = field(default_factory=list)


# ─── helpers ──────────────────────────────────────────────────────────────────

def _load_json(name: str) -> dict:
    return json.loads((_DATA / name).read_text(encoding='utf-8'))


def _beskattningsperiod(year_ends: str) -> str:
    mm = int(year_ends[4:6])
    if   mm <= 4: suffix = 'P1'
    elif mm <= 6: suffix = 'P2'
    elif mm <= 8: suffix = 'P3'
    else:         suffix = 'P4'
    return year_ends[:4] + suffix


def _find_ink2r_field(
    acct: str,
    bas_map: dict,
    sru_override: dict[str, list[str]],
) -> str | None:
    """Return INK2R field code for an account, preferring stored SRU codes."""
    for c in sru_override.get(acct, []):
        if c in bas_map:
            return c
    n = int(acct)
    for fcode, ranges in bas_map.items():
        for r in ranges:
            if r['min'] <= n <= r['max'] and n not in r.get('excl', []):
                return fcode
    return None


def _companion_map(bas_map: dict) -> dict[str, str]:
    """Build {netto_plus_field: netto_minus_field, ...} from bas_ink2r.

    Two fields are companions when they share at least one account number
    and carry opposite netto indicators ('+' vs '-').  This handles cases
    like 7420/7525 (periodiseringsfond) where the range sets differ slightly.
    """
    acct_nettos: dict[int, dict[str, str]] = {}
    for fcode, ranges in bas_map.items():
        for r in ranges:
            netto = r.get('netto')
            if not netto:
                continue
            for n in range(r['min'], r['max'] + 1):
                if n not in r.get('excl', []):
                    if n not in acct_nettos:
                        acct_nettos[n] = {}
                    acct_nettos[n][netto] = fcode
    result: dict[str, str] = {}
    for nettos in acct_nettos.values():
        if '+' in nettos and '-' in nettos:
            result[nettos['+']] = nettos['-']
            result[nettos['-']] = nettos['+']
    return result


# ─── INK2R aggregation ────────────────────────────────────────────────────────

_BS_FIELD_MIN = 7200
_BS_FIELD_MAX = 7399

def _is_bs_field(fcode: str) -> bool:
    return _BS_FIELD_MIN <= int(fcode) <= _BS_FIELD_MAX


def _aggregate_ink2r(
    sie: SIEFile,
    bas_map: dict,
    ink2r_fields_meta: dict,
    warnings: list[str],
    accounting_result: Decimal,
) -> dict[str, Decimal]:
    """Aggregate ledger data into INK2R field amounts (all positive decimals)."""
    sru_override: dict[str, list[str]] = {
        a.number: a.sru for a in sie.accounts if a.sru
    }
    companions = _companion_map(bas_map)

    # Period sums from vouchers
    period: dict[str, Decimal] = {}
    for v in sie.vouchers:
        for t in v.transactions:
            period[t.account] = period.get(t.account, _Z) + t.amount

    raw: dict[str, Decimal] = {}

    # Balance sheet fields — closing balances (UB)
    for acct, ub_val in sie.ub.items():
        if not acct.isdigit():
            continue
        fcode = _find_ink2r_field(acct, bas_map, sru_override)
        if fcode is None or not _is_bs_field(fcode):
            continue
        raw[fcode] = raw.get(fcode, _Z) + abs(ub_val)

    # P&L fields — period sums (exclude 899x which feed into derived result rows)
    unmapped: list[str] = []
    for acct, psum in period.items():
        if not acct.isdigit():
            continue
        n = int(acct)
        if not (3000 <= n <= 8989):
            continue
        fcode = _find_ink2r_field(acct, bas_map, sru_override)
        if fcode is None:
            if psum != _Z:
                unmapped.append(acct)
            continue
        if _is_bs_field(fcode):
            continue  # balance sheet field handled above
        if fcode in ('7450', '7550'):
            continue  # derived; skip

        meta = ink2r_fields_meta.get(fcode, {})
        sign = meta.get('sign', '*')

        # Sign-normalise: produce a positive magnitude for the field
        if sign == '+':
            val = -psum      # income: SIE credit (negative) → negate → positive
        elif sign == '-':
            val = psum       # cost: SIE debit (positive) → use as-is
        else:
            val = abs(psum)  # '*' fields

        # If magnitude is negative (unexpected direction), flip to companion field
        if val < _Z and fcode in companions:
            fcode = companions[fcode]
            val = -val
        elif val < _Z:
            val = _Z  # can't represent; warn
            warnings.append(f'Account {acct}: negative contribution to field {fcode} ignored')

        raw[fcode] = raw.get(fcode, _Z) + val

    if unmapped:
        warnings.append(
            f'P&L accounts with no INK2R field (run `taxreturn annotate`): '
            + ', '.join(sorted(unmapped))
        )

    # Derive 7450/7550 — årets resultat — directly from total P&L period sums
    # (more accurate than summing mapped fields, which may miss unmapped accounts)
    if accounting_result > _Z:
        raw['7450'] = accounting_result
    elif accounting_result < _Z:
        raw['7550'] = -accounting_result

    return {k: v for k, v in raw.items() if v != _Z}


# ─── INK2S computation ────────────────────────────────────────────────────────

def _compute_ink2s(
    sie: SIEFile,
    skatt: SkattBerakning,
    accounting_result: Decimal,
    supplement: dict,
) -> dict[str, Any]:
    """Compute INK2S fields from skatt.py results and supplement YAML."""
    s: dict[str, Any] = {}

    # Fiscal year dates
    s['7011'] = sie.year_begins  # Datum_D YYYYMMDD
    s['7012'] = sie.year_ends

    # 4.1/4.2 Årets resultat — accounting result after all P&L including booked skatt
    # accounting_result = -sum(3000-8989 period) computed in load()
    if accounting_result > _Z:
        s['7650'] = int(accounting_result.to_integral_value())
    elif accounting_result < _Z:
        s['7750'] = int((-accounting_result).to_integral_value())

    # 4.3a Skatt på årets resultat (booked, non-deductible add-back = konto 8910)
    period_8910 = _Z
    for v in sie.vouchers:
        for t in v.transactions:
            if t.account == '8910':
                period_8910 += t.amount
    if period_8910 > _Z:
        s['7651'] = int(period_8910.to_integral_value())

    # 4.6a Schablonintäkt på periodiseringsfonder
    if skatt.schablonintakt > _Z:
        s['7654'] = int(skatt.schablonintakt.to_integral_value())

    # 4.6d Uppräknat belopp vid återföring av periodiseringsfond
    if skatt.total_upprakning > _Z:
        s['7673'] = int(skatt.total_upprakning.to_integral_value())

    # 4.15/4.16 Överskott/Underskott — taxable income
    # skatt.skattbart_resultat = res_fore_skatt + raw_8314 + raw_8423 + schablonintäkt + uppräkning
    # = accounting_result + period_8910 (add-back) + the other adjustments
    # Use it directly since it's the same formula.
    taxable = skatt.skattbart_resultat
    if taxable > _Z:
        s['7670'] = int(taxable.to_integral_value())
    elif taxable < _Z:
        s['7770'] = int((-taxable).to_integral_value())

    # 4.20 Uppdragstagare biträtt (8040 = ja, 8041 = nej)
    if supplement.get('uppdragstagare', True):
        s['8040'] = 'X'
    else:
        s['8041'] = 'X'

    # 4.21 Årsredovisning föremål för revision (8044 = ja, 8045 = nej)
    if supplement.get('revision', False):
        s['8044'] = 'X'
    else:
        s['8045'] = 'X'

    # Manual field overrides from supplement
    for k, v in supplement.get('manual_fields', {}).items():
        s[str(k)] = v

    return s


# ─── public entry point ───────────────────────────────────────────────────────

def load(sie: SIEFile, supplement: dict) -> TaxReturn:
    """Assemble a TaxReturn from an open ledger and supplement YAML."""
    bas_map      = _load_json('bas_ink2r.json')
    ink2r_meta   = _load_json('ink2r_fields.json')
    ink2s_meta   = _load_json('ink2s_fields.json')   # noqa: unused for now
    ink2_meta    = _load_json('ink2_fields.json')

    period_str = _beskattningsperiod(sie.year_ends)
    warnings: list[str] = []

    # Compute period sums once; derive accounting result (3000-8989, negated)
    period: dict[str, Decimal] = {}
    for v in sie.vouchers:
        for t in v.transactions:
            period[t.account] = period.get(t.account, _Z) + t.amount
    accounting_result = -sum(
        v for k, v in period.items()
        if k.isdigit() and 3000 <= int(k) <= 8989
    )

    # INK2R
    ink2r_raw = _aggregate_ink2r(sie, bas_map, ink2r_meta, warnings, accounting_result)

    # Compute bolagsskatt via skatt.py
    skatt = berakna_skatt(
        sie,
        skattesats=Decimal(str(supplement.get('skattesats', '0.206'))),
        statslanerantan=Decimal(str(supplement.get('statslanerantan', '0.025'))),
        year_map={str(k): int(v) for k, v in supplement.get('konto_ar', {}).items()},
    )

    # INK2S
    ink2s_raw = _compute_ink2s(sie, skatt, accounting_result, supplement)

    # INK2 main form — overskott/underskott from INK2S
    ink2_raw: dict[str, Any] = {
        '7011': sie.year_begins,
        '7012': sie.year_ends,
    }
    if '7670' in ink2s_raw:
        ink2_raw['7104'] = ink2s_raw['7670']
    if '7770' in ink2s_raw:
        ink2_raw['7114'] = ink2s_raw['7770']

    return TaxReturn(
        period=period_str,
        org_nr=sie.org_nr,
        company_name=sie.company_name,
        year_begins=sie.year_begins,
        year_ends=sie.year_ends,
        ink2_fields=ink2_raw,
        ink2r_fields=ink2r_raw,
        ink2s_fields=ink2s_raw,
        skatt=skatt,
        warnings=warnings,
    )
