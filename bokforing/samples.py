"""Sample voucher store for AI account-selection hints.

Stored as samples.json in the same directory as the ledger file.
The file is not year-specific — patterns apply across all fiscal years.
"""
from __future__ import annotations

import json
import os


def _samples_path(ledger_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(ledger_path)), 'samples.json')


def _load(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return data.get('samples', [])


def _save(path: str, samples: list[dict]) -> None:
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'samples': samples}, f, ensure_ascii=False, indent=2)


def _next_id(samples: list[dict]) -> int:
    if not samples:
        return 1
    return max(s['id'] for s in samples) + 1


def list_samples(ledger_path: str) -> list[dict]:
    return _load(_samples_path(ledger_path))


def get_sample(ledger_path: str, sample_id: int) -> dict | None:
    return next((s for s in list_samples(ledger_path) if s['id'] == sample_id), None)


def add_sample(ledger_path: str, description: str,
               transactions: list[dict], notes: str = '') -> dict:
    path = _samples_path(ledger_path)
    samples = _load(path)
    sample: dict = {
        'id': _next_id(samples),
        'description': description,
        'transactions': [
            {k: v for k, v in t.items() if k in ('account', 'amount', 'label') and v != ''}
            for t in transactions
        ],
    }
    if notes:
        sample['notes'] = notes
    samples.append(sample)
    _save(path, samples)
    return sample


def delete_sample(ledger_path: str, sample_id: int) -> bool:
    path = _samples_path(ledger_path)
    samples = _load(path)
    new_samples = [s for s in samples if s['id'] != sample_id]
    if len(new_samples) == len(samples):
        return False
    _save(path, new_samples)
    return True


def format_for_ai(samples: list[dict], account_map: dict) -> str:
    """Format samples as text for inclusion in the AI system prompt."""
    if not samples:
        return ''
    lines = ['Example vouchers (curated account patterns — use these to guide account selection):']
    for s in samples:
        notes_suffix = f'  # {s["notes"]}' if s.get('notes') else ''
        lines.append(f'  {s["description"]}{notes_suffix}')
        for t in s['transactions']:
            acc_obj = account_map.get(t['account'])
            acc_label = acc_obj.label if acc_obj is not None else ''
            lbl = f'  ({t["label"]})' if t.get('label') else ''
            lines.append(f'    {t["account"]}  {str(t["amount"]):>12}  {acc_label}{lbl}')
    return '\n'.join(lines)
