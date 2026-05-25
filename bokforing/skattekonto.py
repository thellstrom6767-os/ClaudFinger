"""Import and AI-suggest vouchers from a Skatteverket skattekonto CSV export.

CSV format (semicolon-delimited, UTF-8 BOM, quoted fields):
  Row 1 : company name ; org nr ; "" ; ""
  Row 2 : empty
  Row 3 : "" ; "Ingående saldo YYYY-MM-DD" ; "" ; balance
  Row N : "YYYY-MM-DD" ; "description" ; "amount" ; "running balance"
  Last  : "" ; "Utgående saldo YYYY-MM-DD" ; "" ; balance

Amounts use space as thousands separator and may be negative.
"""
from __future__ import annotations

import csv
import os
from decimal import Decimal, InvalidOperation
from typing import Optional

from .models import SIEFile


# ─── CSV parsing ─────────────────────────────────────────────────────────────

def _dec(s: str) -> Optional[Decimal]:
    """'10 500' → Decimal('10500'),  '-4 797' → Decimal('-4797')."""
    s = s.strip().replace('\xa0', '').replace(' ', '').replace(' ', '')
    if not s:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def parse_csv(
    path: str,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> tuple[Decimal, list[dict]]:
    """Parse a skattekonto CSV export.

    from_date / to_date : 'YYYY-MM-DD', inclusive.  None = no limit.

    Returns (opening_balance, transactions) where each transaction is:
      date         str  YYYYMMDD
      date_display str  YYYY-MM-DD
      description  str
      amount       Decimal
      balance      Decimal  (running balance after this transaction)
    """
    opening_balance = Decimal('0')
    transactions: list[dict] = []

    with open(path, encoding='utf-8-sig') as f:
        reader = csv.reader(f, delimiter=';', quotechar='"')
        for row in reader:
            if len(row) < 4:
                continue
            date_s = row[0].strip()
            desc   = row[1].strip()
            amt_s  = row[2].strip()
            bal_s  = row[3].strip()

            if not date_s:
                # Opening/closing balance meta-rows
                if 'Ingående saldo' in desc:
                    b = _dec(bal_s)
                    if b is not None:
                        opening_balance = b
                continue

            if not date_s[0].isdigit():
                continue

            amount = _dec(amt_s)
            if amount is None:
                continue

            if from_date and date_s < from_date:
                continue
            if to_date and date_s > to_date:
                continue

            transactions.append({
                'date':         date_s.replace('-', ''),
                'date_display': date_s,
                'description':  desc,
                'amount':       amount,
                'balance':      _dec(bal_s) or Decimal('0'),
            })

    return opening_balance, transactions


# ─── AI batch suggestion ──────────────────────────────────────────────────────

_TOOL: dict = {
    'name': 'suggest_vouchers',
    'description': (
        'Suggest double-entry vouchers for a list of skattekonto transactions.'
    ),
    'input_schema': {
        'type': 'object',
        'required': ['vouchers'],
        'properties': {
            'vouchers': {
                'type': 'array',
                'description': (
                    'One entry per input transaction, in the same order.'
                ),
                'items': {
                    'type': 'object',
                    'required': ['row_index', 'date', 'description',
                                 'confidence', 'transactions'],
                    'properties': {
                        'row_index': {
                            'type': 'integer',
                            'description': (
                                'Zero-based index matching the input row.'
                            ),
                        },
                        'date': {
                            'type': 'string',
                            'description': 'YYYYMMDD.',
                        },
                        'description': {
                            'type': 'string',
                            'description': 'Short voucher description.',
                        },
                        'confidence': {
                            'type': 'string',
                            'enum': ['high', 'medium', 'low'],
                        },
                        'transactions': {
                            'type': 'array',
                            'description': (
                                'Double-entry lines summing to zero. '
                                'Debit = positive, credit = negative.'
                            ),
                            'items': {
                                'type': 'object',
                                'required': ['account', 'amount'],
                                'properties': {
                                    'account': {'type': 'string'},
                                    'amount':  {'type': 'string'},
                                    'label':   {'type': 'string'},
                                },
                            },
                        },
                        'notes': {
                            'type': 'string',
                            'description': (
                                'Explanation of accounting treatment chosen.'
                            ),
                        },
                    },
                },
            },
        },
    },
}


def suggest_vouchers(
    transactions: list[dict],
    sie: SIEFile,
    opening_balance: Decimal,
    samples: list[dict] | None = None,
) -> list[dict]:
    """Send all transactions to Claude and return a list of voucher suggestions.

    Each suggestion mirrors the structure returned by ai.suggest_voucher():
      row_index, date, description, confidence, transactions, notes.
    Transaction amounts are normalised to Decimal.

    Raises EnvironmentError if ANTHROPIC_API_KEY is not set.
    Raises RuntimeError on unexpected API response.
    """
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        raise EnvironmentError(
            'ANTHROPIC_API_KEY is not set.\n'
            'Export it before running: export ANTHROPIC_API_KEY=sk-ant-…'
        )

    import anthropic
    from .ai import MODEL, _context_accounts

    client = anthropic.Anthropic(api_key=api_key)

    # Build a readable transaction table for Claude
    rows_text = '\n'.join(
        f'  [{i}]  {t["date_display"]}  {t["amount"]:>10}  {t["description"]}'
        for i, t in enumerate(transactions)
    )

    # Current 1630 balance in ledger for cross-reference
    from .ledger import get_balances
    balances = get_balances(sie)
    ledger_1630 = balances.get('1630', Decimal('0'))

    accounts_text = _context_accounts(sie)

    from .samples import format_for_ai as _fmt_samples
    samples_text = _fmt_samples(samples or [], sie.account_map()) if samples else ''

    system = f"""You are a Swedish accounting assistant.

Company: {sie.company_name} ({sie.org_nr})
Fiscal year: {sie.year_begins[:4]}

You will be given transactions from the company's skattekonto statement
(account 1630 — Avräkning för skatter och avgifter).

Current ledger balance of 1630: {ledger_1630}
Opening balance in CSV statement: {opening_balance}

Relevant accounts:
{accounts_text}

{samples_text}

Common skattekonto accounting treatments (Swedish BAS):
  Intäktsränta (positive)         → 1630 debit / 8314 credit
  Kostnadsränta (negative)        → 1630 credit / 8423 debit
  Korrigerad intäktsränta         → reverse of Intäktsränta
  Korrigerad kostnadsränta        → reverse of Kostnadsränta
  Debiterad preliminärskatt (neg) → 1630 credit / 2519 debit
  Slutlig skatt (negative)        → 1630 credit / 2512 debit (use current 2512 balance context)
  Inbetalning bokförd (positive)  → 1630 debit / 1941 credit
  Moms … (negative)               → 1630 credit / 2650 debit

SIE sign convention:
  Debit = positive  (asset increases, expense incurred)
  Credit = negative (liability increases, income earned)
All transactions in a voucher must sum to exactly zero.

Return one voucher suggestion per input row, in the same order,
using the row_index field to identify each row.
If a transaction type is ambiguous, set confidence to "medium" or "low"
and explain in notes."""

    user_msg = f"""Please suggest vouchers for these {len(transactions)} skattekonto transactions:

{rows_text}

Return one suggestion per row (row_index 0 to {len(transactions) - 1})."""

    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=system,
        tools=[_TOOL],
        tool_choice={'type': 'tool', 'name': 'suggest_vouchers'},
        messages=[{'role': 'user', 'content': user_msg}],
    )

    for block in response.content:
        if block.type == 'tool_use' and block.name == 'suggest_vouchers':
            suggestions = block.input.get('vouchers', [])
            # Normalise amounts to Decimal
            for s in suggestions:
                for t in s.get('transactions', []):
                    raw = str(t.get('amount', '0')).replace(',', '.')
                    try:
                        t['amount'] = Decimal(raw)
                    except InvalidOperation:
                        t['amount'] = Decimal('0')
                    t.setdefault('label', '')
            return suggestions

    raise RuntimeError('Claude did not return voucher suggestions.')
