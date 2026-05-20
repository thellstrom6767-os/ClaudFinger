from __future__ import annotations
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional


@dataclass
class Transaction:
    account: str
    amount: Decimal
    date: str       # YYYYMMDD
    label: str = ''


@dataclass
class Voucher:
    series: str
    number: int
    date: str       # YYYYMMDD
    label: str
    reg_date: str   # YYYYMMDD
    signature: str
    transactions: list[Transaction] = field(default_factory=list)

    def total(self) -> Decimal:
        return sum((t.amount for t in self.transactions), Decimal('0'))


@dataclass
class Account:
    number: str
    label: str
    ktyp: Optional[str] = None
    sru: list[str] = field(default_factory=list)


@dataclass
class SIEFile:
    program: str = "Claude's converter"
    program_version: str = ''
    gen_date: str = ''
    gen_author: str = ''
    org_nr: str = ''
    company_name: str = ''
    contact: str = ''
    street: str = ''
    zip_city: str = ''
    phone: str = ''
    year_begins: str = ''
    year_ends: str = ''
    currency: str = 'SEK'
    accounts: list[Account] = field(default_factory=list)
    ib: dict[str, Decimal] = field(default_factory=dict)
    ub: dict[str, Decimal] = field(default_factory=dict)
    res: dict[str, Decimal] = field(default_factory=dict)
    vouchers: list[Voucher] = field(default_factory=list)

    def account_map(self) -> dict[str, Account]:
        return {a.number: a for a in self.accounts}
