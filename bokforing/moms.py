"""Swedish VAT (moms) declaration calculation."""
from __future__ import annotations
from decimal import Decimal, ROUND_HALF_UP
from typing import NamedTuple

from .models import SIEFile

Z = Decimal('0')
ONE = Decimal('1')
VAT_ACCOUNTS = ('2610', '2620', '2630')


def _is_taxable_income(account: str) -> bool:
    """3xxx income accounts, excluding exempt (3040-3049) and rounding (3740)."""
    if not account.isdigit():
        return False
    n = int(account)
    return 3000 <= n < 4000 and not (3040 <= n <= 3049) and n != 3740


def _is_exempt_income(account: str) -> bool:
    """3040-3049: momsfri försäljning."""
    if not account.isdigit():
        return False
    return 3040 <= int(account) <= 3049


# BAS last-digit convention for accounts 3001-3039:
#   last digit 1 → 25 % (2610), 2 → 12 % (2620), 3 → 6 % (2630)
_BAS_DIGIT: dict[int, str] = {1: '2610', 2: '2620', 3: '2630'}


def _bas_vat_account(account: str) -> str | None:
    """Return the moms account prescribed by BAS, or None if the convention
    does not apply (account outside 3001-3039, or last digit not 1/2/3)."""
    if not account.isdigit():
        return None
    n = int(account)
    if 3001 <= n <= 3039:
        return _BAS_DIGIT.get(n % 10)
    return None


def _build_income_vat_mapping(sie: SIEFile) -> dict[str, str]:
    """Map each taxable income account to its moms account.

    Primary rule: BAS last-digit convention (3001-3039, digit 1/2/3 → 25%/12%/6%).
    Fallback: highest cumulative co-occurrence with a moms account across all
    vouchers in the ledger, for accounts outside the BAS pattern (e.g. 3030).
    Accounts with neither a BAS rule nor any co-occurrence are left unmapped.
    """
    # Collect all taxable income accounts seen in the ledger
    all_income: set[str] = set()
    for v in sie.vouchers:
        for t in v.transactions:
            if _is_taxable_income(t.account):
                all_income.add(t.account)

    # Co-occurrence totals for fallback
    co_totals: dict[tuple[str, str], Decimal] = {}
    for v in sie.vouchers:
        moms_in_v = {t.account for t in v.transactions if t.account in VAT_ACCOUNTS}
        if not moms_in_v:
            continue
        for t in v.transactions:
            if _is_taxable_income(t.account) and t.amount < Z:
                for moms_acct in moms_in_v:
                    key = (t.account, moms_acct)
                    co_totals[key] = co_totals.get(key, Z) + abs(t.amount)

    mapping: dict[str, str] = {}
    for inc_acct in all_income:
        bas = _bas_vat_account(inc_acct)
        if bas is not None:
            mapping[inc_acct] = bas
        else:
            candidates = [(moms, co_totals.get((inc_acct, moms), Z)) for moms in VAT_ACCOUNTS]
            best_moms, best_total = max(candidates, key=lambda x: x[1])
            if best_total > Z:
                mapping[inc_acct] = best_moms
    return mapping


def _period_sum(sie: SIEFile, account: str, from_date: str, to_date: str) -> Decimal:
    total = Z
    for v in sie.vouchers:
        for t in v.transactions:
            if t.account == account and from_date <= t.date <= to_date:
                total += t.amount
    return total


def _taxable_bases(sie: SIEFile, from_date: str, to_date: str,
                   mapping: dict[str, str]) -> dict[str, Decimal]:
    """Sum taxable income per moms account for the period using the pre-built mapping."""
    totals: dict[str, Decimal] = {a: Z for a in VAT_ACCOUNTS}
    for v in sie.vouchers:
        for t in v.transactions:
            if (_is_taxable_income(t.account)
                    and t.account in mapping
                    and from_date <= t.date <= to_date
                    and t.amount < Z):
                totals[mapping[t.account]] += abs(t.amount)
    return totals


def _exempt_income(sie: SIEFile, from_date: str, to_date: str) -> Decimal:
    """Sum 3040-3049 over the period. Returns positive value."""
    total = Z
    for v in sie.vouchers:
        for t in v.transactions:
            if (_is_exempt_income(t.account)
                    and from_date <= t.date <= to_date
                    and t.amount < Z):
                total += t.amount
    return -total


class MomsBerakning(NamedTuple):
    from_date: str
    to_date: str
    # Raw period sums, SIE sign (utgående < 0, ingående > 0)
    raw_2610: Decimal
    raw_2620: Decimal
    raw_2630: Decimal
    raw_2640: Decimal
    # Exact income bases (positive display values)
    base_utg_25: Decimal   # Ruta 05 exact
    base_utg_12: Decimal   # Ruta 06 exact
    base_utg_6: Decimal    # Ruta 07 exact
    base_momsfri: Decimal  # Ruta 08 exact
    # Declaration amounts: whole SEK, ROUND_HALF_UP, always >= 0
    dec_base_25: Decimal       # Ruta 05
    dec_base_12: Decimal       # Ruta 06
    dec_base_6: Decimal        # Ruta 07
    dec_base_momsfri: Decimal  # Ruta 08
    dec_utg_25: Decimal        # Ruta 10
    dec_utg_12: Decimal        # Ruta 11
    dec_utg_6: Decimal         # Ruta 12
    dec_ing: Decimal           # Ruta 30
    dec_netto: Decimal         # Ruta 49 (positive = to pay, negative = to receive)
    # Voucher line amounts (SIE sign convention)
    amount_2650: Decimal   # = -dec_netto
    amount_3740: Decimal   # rounding residual; 0 when exact


def berakna_moms(sie: SIEFile, from_date: str, to_date: str) -> MomsBerakning:
    raw_2610 = _period_sum(sie, '2610', from_date, to_date)
    raw_2620 = _period_sum(sie, '2620', from_date, to_date)
    raw_2630 = _period_sum(sie, '2630', from_date, to_date)
    raw_2640 = _period_sum(sie, '2640', from_date, to_date)

    mapping = _build_income_vat_mapping(sie)
    bases = _taxable_bases(sie, from_date, to_date, mapping)
    base_utg_25  = bases['2610']
    base_utg_12  = bases['2620']
    base_utg_6   = bases['2630']
    base_momsfri = _exempt_income(sie, from_date, to_date)

    dec_base_25      = base_utg_25.quantize(ONE, rounding=ROUND_HALF_UP)
    dec_base_12      = base_utg_12.quantize(ONE, rounding=ROUND_HALF_UP)
    dec_base_6       = base_utg_6.quantize(ONE, rounding=ROUND_HALF_UP)
    dec_base_momsfri = base_momsfri.quantize(ONE, rounding=ROUND_HALF_UP)

    dec_utg_25 = abs(raw_2610).quantize(ONE, rounding=ROUND_HALF_UP)
    dec_utg_12 = abs(raw_2620).quantize(ONE, rounding=ROUND_HALF_UP)
    dec_utg_6  = abs(raw_2630).quantize(ONE, rounding=ROUND_HALF_UP)
    dec_ing    = abs(raw_2640).quantize(ONE, rounding=ROUND_HALF_UP)
    dec_netto  = dec_utg_25 + dec_utg_12 + dec_utg_6 - dec_ing

    amount_2650 = -dec_netto

    # For balance: clearing_sum + amount_2650 + amount_3740 = 0
    clearing_sum = -raw_2610 - raw_2620 - raw_2630 - raw_2640
    amount_3740 = -(clearing_sum + amount_2650)

    return MomsBerakning(
        from_date=from_date,
        to_date=to_date,
        raw_2610=raw_2610,
        raw_2620=raw_2620,
        raw_2630=raw_2630,
        raw_2640=raw_2640,
        base_utg_25=base_utg_25,
        base_utg_12=base_utg_12,
        base_utg_6=base_utg_6,
        base_momsfri=base_momsfri,
        dec_base_25=dec_base_25,
        dec_base_12=dec_base_12,
        dec_base_6=dec_base_6,
        dec_base_momsfri=dec_base_momsfri,
        dec_utg_25=dec_utg_25,
        dec_utg_12=dec_utg_12,
        dec_utg_6=dec_utg_6,
        dec_ing=dec_ing,
        dec_netto=dec_netto,
        amount_2650=amount_2650,
        amount_3740=amount_3740,
    )
