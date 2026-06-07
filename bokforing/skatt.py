"""Swedish corporate income tax (bolagsskatt) calculation."""
from __future__ import annotations
from decimal import Decimal, ROUND_FLOOR
from typing import NamedTuple

from .models import SIEFile
from .ledger import get_account_history

Z = Decimal('0')


class UpprakningPost(NamedTuple):
    account: str
    income_year: int
    factor: Decimal
    aterfort: Decimal       # positive (debit on 212x = återföring)
    upprakning: Decimal     # positive addition to taxable income


class SkattBerakning(NamedTuple):
    res_fore_skatt: Decimal     # display convention: positive = profit
    raw_8314: Decimal           # SIE-sign period sum on 8314 (neg if income received)
    raw_8423: Decimal           # SIE-sign period sum on 8423 (pos if expense incurred)
    pf_ib_total: Decimal        # IB sum on all pf accounts (neg if reserves exist)
    statslanerantan: Decimal
    schablonintakt: Decimal     # positive; added to taxable income
    upprakning_posts: list[UpprakningPost]
    total_upprakning: Decimal
    skattbart_resultat: Decimal     # raw taxable income before rounding
    skattbart_avrundat: Decimal     # rounded down to nearest 10 SEK
    skattesats: Decimal
    bolagsskatt_beraknad: Decimal   # skattbart_avrundat × skattesats, 2 dp
    bolagsskatt: Decimal            # rounded down to nearest 1 SEK


def _income_year(account_nr: str, year_map: dict[str, int]) -> int | None:
    if account_nr in year_map:
        return year_map[account_nr]
    nr = int(account_nr)
    if 2110 <= nr <= 2129:
        return 2000 + (nr % 100)
    return None


def _upprakning_factor(income_year: int) -> Decimal:
    """Mandatory scale-up factor when reversing old periodiseringsfonder.

    Introduced when bolagsskatt was lowered in two steps (22%→21.4%→20.6%)
    to neutralise the arbitrage from deducting at a higher rate.
    """
    if income_year <= 2018:
        return Decimal('1.06')
    elif income_year <= 2020:
        return Decimal('1.04')
    return Decimal('1.00')


def berakna_skatt(
    sie: SIEFile,
    skattesats: Decimal,
    statslanerantan: Decimal,
    year_map: dict[str, int],
) -> SkattBerakning:
    # Period movements per account, raw SIE signs
    period: dict[str, Decimal] = {}
    for v in sie.vouchers:
        for t in v.transactions:
            period[t.account] = period.get(t.account, Z) + t.amount

    # Resultat före skatt: negate raw P&L sum so positive = profit
    res_fore_skatt = sum(
        -v for k, v in period.items()
        if k.isdigit() and 3000 <= int(k) <= 8899
    )

    # 8314 raw period (negative = credit = income received)
    # Adding it to taxable removes the tax-free income from the base
    raw_8314 = period.get('8314', Z)

    # 8423 raw period (positive = debit = expense incurred)
    # Adding it to taxable adds back the non-deductible expense
    raw_8423 = period.get('8423', Z)

    # Periodiseringsfond accounts: 2110-2129 plus any year_map overrides
    pf_nrs = sorted(
        {a.number for a in sie.accounts
         if a.number.isdigit() and 2110 <= int(a.number) <= 2129}
        | set(year_map)
    )

    # Schablonintäkt: based on IB (opening balance) of pf accounts.
    # IB is negative (credit balances = reserves), so negate to get positive base.
    pf_ib_total = sum(sie.ib.get(nr, Z) for nr in pf_nrs)
    schablonintakt = (-pf_ib_total * statslanerantan).quantize(Decimal('0.01'))

    # Uppräkning: for each vintage with factor > 1, sum positive TRANS (återföringar)
    upprakning_posts: list[UpprakningPost] = []
    for nr in pf_nrs:
        iy = _income_year(nr, year_map)
        if iy is None:
            continue
        factor = _upprakning_factor(iy)
        if factor == Decimal('1.00'):
            continue
        history = get_account_history(sie, nr)
        aterfort = sum(t.amount for _, t in history if t.amount > Z)
        if aterfort == Z:
            continue
        upprakning = (aterfort * (factor - Decimal('1'))).quantize(Decimal('0.01'))
        upprakning_posts.append(UpprakningPost(nr, iy, factor, aterfort, upprakning))

    total_upprakning = sum(p.upprakning for p in upprakning_posts)

    # Taxable income in display convention:
    #   raw_8314 (negative) removes the tax-free interest income from the base
    #   raw_8423 (positive) adds back the non-deductible interest expense
    skattbart = (res_fore_skatt
                 + raw_8314
                 + raw_8423
                 + schablonintakt
                 + total_upprakning)

    skattbart_avrundat = (skattbart.quantize(Decimal('10'), rounding=ROUND_FLOOR)
                          if skattbart > Z else skattbart)

    bolagsskatt_beraknad = ((skattbart_avrundat * skattesats).quantize(Decimal('0.01'))
                            if skattbart_avrundat > Z else Z)

    bolagsskatt = (bolagsskatt_beraknad.to_integral_value(rounding=ROUND_FLOOR)
                   if bolagsskatt_beraknad > Z else Z)

    return SkattBerakning(
        res_fore_skatt=res_fore_skatt,
        raw_8314=raw_8314,
        raw_8423=raw_8423,
        pf_ib_total=pf_ib_total,
        statslanerantan=statslanerantan,
        schablonintakt=schablonintakt,
        upprakning_posts=upprakning_posts,
        total_upprakning=total_upprakning,
        skattbart_resultat=skattbart,
        skattbart_avrundat=skattbart_avrundat,
        skattesats=skattesats,
        bolagsskatt_beraknad=bolagsskatt_beraknad,
        bolagsskatt=bolagsskatt,
    )
