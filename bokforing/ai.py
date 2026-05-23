"""AI-assisted voucher suggestion using the Anthropic SDK.

Sends a receipt or invoice image/PDF to Claude and receives a structured
double-entry voucher suggestion. The caller is responsible for presenting
the suggestion to the user and obtaining sign-off before saving.
"""
from __future__ import annotations

import base64
import os
from decimal import Decimal, InvalidOperation

from .models import SIEFile

MODEL = 'claude-sonnet-4-6'

# Account ranges to include in the context sent to Claude
_CONTEXT_RANGES = [
    (1900, 2000),   # kassa och bank
    (2400, 3000),   # kortfristiga skulder (inkl moms)
    (3000, 4000),   # intäkter
    (4000, 5000),   # material och varor
    (5000, 7000),   # externa kostnader
    (7000, 8000),   # personalkostnader och avskrivningar
    (8300, 8500),   # finansiella poster
]

_TOOL = {
    'name': 'suggest_voucher',
    'description': (
        'Suggest how to book this document as a balanced double-entry '
        "voucher in a Swedish company's accounts."
    ),
    'input_schema': {
        'type': 'object',
        'required': ['date', 'description', 'confidence', 'transactions'],
        'properties': {
            'date': {
                'type': 'string',
                'description': 'Transaction date from the document as YYYYMMDD.',
            },
            'description': {
                'type': 'string',
                'description': 'Short voucher description: vendor name + purpose.',
            },
            'confidence': {
                'type': 'string',
                'enum': ['high', 'medium', 'low'],
                'description': 'Confidence in the suggestion.',
            },
            'transactions': {
                'type': 'array',
                'description': (
                    'Double-entry transaction lines. '
                    'Amounts must sum to exactly zero.'
                ),
                'items': {
                    'type': 'object',
                    'required': ['account', 'amount'],
                    'properties': {
                        'account': {
                            'type': 'string',
                            'description': '4-digit BAS account number.',
                        },
                        'amount': {
                            'type': 'string',
                            'description': (
                                'Amount with 2 decimal places. '
                                'Debit = positive, credit = negative.'
                            ),
                        },
                        'label': {
                            'type': 'string',
                            'description': 'Optional transaction label.',
                        },
                    },
                },
            },
            'notes': {
                'type': 'string',
                'description': (
                    'Brief explanation of the accounting treatment chosen, '
                    'including VAT rate applied and any assumptions made.'
                ),
            },
        },
    },
}


def _context_accounts(sie: SIEFile) -> str:
    lines = []
    for acc in sie.accounts:
        if not acc.number.isdigit():
            continue
        n = int(acc.number)
        for lo, hi in _CONTEXT_RANGES:
            if lo <= n < hi:
                lines.append(f'  {acc.number}  {acc.label}')
                break
    return '\n'.join(lines)


def _recent_vouchers_text(sie: SIEFile, n: int = 5) -> str:
    if not sie.vouchers:
        return ''
    acc_map = {a.number: a.label for a in sie.accounts}
    lines = ['Recent vouchers (for pattern recognition only):']
    for v in sie.vouchers[-n:]:
        lines.append(f'  {v.date}  {v.label}')
        for t in v.transactions:
            lines.append(
                f'    {t.account} {acc_map.get(t.account, "")}: {t.amount}'
            )
    return '\n'.join(lines)


def _encode(path: str) -> tuple[str, str]:
    """Return (media_type, base64_data)."""
    ext = os.path.splitext(path)[1].lower()
    mt = {
        '.pdf':  'application/pdf',
        '.jpg':  'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png':  'image/png',
        '.tif':  'image/tiff',
        '.tiff': 'image/tiff',
    }.get(ext, 'application/octet-stream')
    with open(path, 'rb') as f:
        return mt, base64.standard_b64encode(f.read()).decode()


def suggest_voucher(file_path: str, sie: SIEFile) -> dict:
    """Analyse a receipt/invoice and return a structured voucher suggestion.

    Returns a dict with keys:
      date          str  YYYYMMDD
      description   str
      confidence    str  'high' | 'medium' | 'low'
      transactions  list of {'account', 'amount': Decimal, 'label'}
      notes         str  (may be absent)

    Raises RuntimeError on API failure.
    Raises EnvironmentError if ANTHROPIC_API_KEY is not set.
    """
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        raise EnvironmentError(
            'ANTHROPIC_API_KEY environment variable is not set.\n'
            'Export it before running: export ANTHROPIC_API_KEY=sk-ant-…'
        )

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)

    accounts_text = _context_accounts(sie)
    recent_text   = _recent_vouchers_text(sie)

    system = f"""You are a Swedish accounting assistant helping to book documents.

Company: {sie.company_name} ({sie.org_nr})
Fiscal year: {sie.year_begins[:4]}

Available accounts (BAS chart, filtered):
{accounts_text}

Swedish VAT rates:
  25%  standard  → ingående moms to 2640, utgående to 2610
  12%  food/hotel → 2640 / 2620
   6%  books/transport → 2640 / 2630

SIE sign convention (use this exactly):
  Debit  = positive  (asset increases, expense incurred)
  Credit = negative  (liability increases, income earned)
All transaction amounts in a voucher must sum to exactly zero.

{recent_text}

Rules:
- Extract the date from the document itself; do not use today's date.
- If a value is unclear set confidence to "low" and explain in notes.
- Never guess amounts. If the total is unclear, say so in notes.
- Use tool suggest_voucher to return your answer."""

    media_type, b64 = _encode(file_path)
    doc_block = (
        {'type': 'document',
         'source': {'type': 'base64', 'media_type': media_type, 'data': b64}}
        if media_type == 'application/pdf'
        else
        {'type': 'image',
         'source': {'type': 'base64', 'media_type': media_type, 'data': b64}}
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=system,
        tools=[_TOOL],
        tool_choice={'type': 'tool', 'name': 'suggest_voucher'},
        messages=[{
            'role': 'user',
            'content': [
                doc_block,
                {'type': 'text',
                 'text': 'Analyse this document and suggest how to book it.'},
            ],
        }],
    )

    for block in response.content:
        if block.type == 'tool_use' and block.name == 'suggest_voucher':
            result = dict(block.input)
            for t in result.get('transactions', []):
                raw = str(t.get('amount', '0')).replace(',', '.')
                try:
                    t['amount'] = Decimal(raw)
                except InvalidOperation:
                    t['amount'] = Decimal('0')
                t.setdefault('label', '')
            return result

    raise RuntimeError('Claude did not return a voucher suggestion.')
