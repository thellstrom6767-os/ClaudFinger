"""Accounting logic: balance computation, account lookup, year initialisation."""
from __future__ import annotations
import copy
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


def closing_balances(prev: SIEFile) -> tuple[dict[str, Decimal], str]:
    """Return (balance_sheet_closing_balances, source_description).

    Uses #UB entries when the year is closed; otherwise computes from
    IB + transactions. Only returns balance-sheet accounts (1xxx, 2xxx).
    """
    if prev.ub:
        raw = prev.ub
        source = '#UB entries (closed year)'
    else:
        raw = get_balances(prev)
        source = 'computed from IB + transactions (open year)'

    bs = {k: v for k, v in raw.items()
          if k.isdigit() and int(k) < 3000 and v != Decimal('0')}
    return bs, source


def init_from_previous(prev: SIEFile, new_begins: str, new_ends: str) -> tuple[SIEFile, str]:
    """Create a new-year SIEFile carrying forward closing balances as opening balances.

    Returns (new_sie, source_description).
    """
    ib, source = closing_balances(prev)
    new_sie = SIEFile(
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
        ib=ib,
    )
    return new_sie, source


# ─────────────────────────────────────────────────────────────────────────────

RenumberMap = dict[tuple[str, int], tuple[str, int]]  # (series,old) → (series,new)


def sort_vouchers(sie: SIEFile, key: str = 'reg_date') -> tuple[SIEFile, RenumberMap]:
    """Sort vouchers within each series and renumber them 1, 2, 3, …

    key: 'reg_date' — sort by registration date (when the entry was made)
         'date'     — sort by voucher date (when the transaction occurred)

    Within a series, vouchers that share the same sort key retain their
    original relative order (stable sort).  Vouchers whose sort key is
    empty sort last.

    Returns (new_sie, renumber_map) where renumber_map maps every voucher
    whose number changed: {(series, old_number): (series, new_number)}.
    """
    new_sie = copy.copy(sie)
    new_sie.accounts = list(sie.accounts)
    new_sie.ib = dict(sie.ib)
    new_sie.ub = dict(sie.ub)
    new_sie.res = dict(sie.res)

    renumber_map: RenumberMap = {}
    new_vouchers: list[Voucher] = []

    series_groups: dict[str, list[Voucher]] = {}
    for v in sie.vouchers:
        series_groups.setdefault(v.series, []).append(v)

    for series_id in sorted(series_groups):
        if key == 'reg_date':
            # Empty reg_date sorts last; use original number as stable tiebreak
            sorted_vs = sorted(
                series_groups[series_id],
                key=lambda v: (v.reg_date or '\xff', v.number),
            )
        else:
            sorted_vs = sorted(
                series_groups[series_id],
                key=lambda v: (v.date or '\xff', v.number),
            )

        for new_num, v in enumerate(sorted_vs, start=1):
            if v.number != new_num:
                renumber_map[(series_id, v.number)] = (series_id, new_num)
            new_v = copy.copy(v)
            new_v.transactions = list(v.transactions)
            new_v.number = new_num
            new_vouchers.append(new_v)

    new_sie.vouchers = new_vouchers
    return new_sie, renumber_map
