"""Accounting logic: balance computation, account lookup, year initialisation."""
from __future__ import annotations
from datetime import date
from decimal import Decimal

from .models import Account, SIEFile, Voucher
from .sie import PROGRAM_NAME, PROGRAM_VERSION


def get_balances(sie: SIEFile) -> dict[str, Decimal]:
    """Running balances = IB + all posted transactions. Zero balances excluded."""
    balances: dict[str, Decimal] = dict(sie.ib)
    for v in sie.vouchers:
        for t in v.transactions:
            balances[t.account] = balances.get(t.account, Decimal('0')) + t.amount
    return {k: v for k, v in balances.items() if v != Decimal('0')}


def get_account_history(sie: SIEFile, account: str) -> list[tuple[Voucher, object]]:
    """All (voucher, transaction) pairs for a given account number."""
    return [
        (v, t)
        for v in sie.vouchers
        for t in v.transactions
        if t.account == account
    ]


def next_voucher_number(sie: SIEFile, series: str = 'A') -> int:
    nums = [v.number for v in sie.vouchers if v.series == series]
    return max(nums, default=0) + 1


def find_account(sie: SIEFile, query: str) -> Account | None:
    """Find account by exact number or case-insensitive label substring."""
    for acc in sie.accounts:
        if acc.number == query:
            return acc
    q = query.lower()
    for acc in sie.accounts:
        if q in acc.label.lower():
            return acc
    return None


def init_from_previous(prev: SIEFile, new_begins: str, new_ends: str) -> SIEFile:
    """Create a new-year SIEFile carrying forward closing balances as opening balances."""
    return SIEFile(
        program=PROGRAM_NAME,
        program_version=PROGRAM_VERSION,
        gen_date=date.today().strftime('%Y%m%d'),
        gen_author=prev.gen_author,
        org_nr=prev.org_nr,
        company_name=prev.company_name,
        contact=prev.contact,
        street=prev.street,
        zip_city=prev.zip_city,
        phone=prev.phone,
        year_begins=new_begins,
        year_ends=new_ends,
        currency=prev.currency,
        accounts=list(prev.accounts),
        ib=dict(prev.ub),   # previous year's closing = new year's opening
    )
