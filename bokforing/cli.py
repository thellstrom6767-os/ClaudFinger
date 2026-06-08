"""CLI commands for the bokforing accounting app."""
from __future__ import annotations
import glob
import os
import re
import subprocess
import sys
from datetime import date
from decimal import Decimal, InvalidOperation

import click

from . import underlag as underlag_module
from . import samples as samples_module
from . import store as store_module
from .reports import generate_balansrapport, generate_resultatrapport

from . import sie as sie_module
from .ledger import (canonical_ib_text, canonical_voucher_text,
                     delete_voucher as _delete_voucher_logic, find_account,
                     get_account_history, get_balances, init_from_previous,
                     next_voucher_number, sort_vouchers)
from .models import Transaction, Voucher


def _today() -> str:
    return date.today().strftime('%Y%m%d')


def _fmt_date(d: str) -> str:
    if len(d) == 8:
        return f'{d[:4]}-{d[4:6]}-{d[6:]}'
    return d


def _fmt_amount(amount: Decimal) -> str:
    return f'{amount:>14,.2f}'


def _acc_name(account_map: dict, acct: str) -> str:
    acc = account_map.get(acct)
    return acc.label if acc else ''


def _resolve_ledger(ctx_obj: dict) -> str:
    path = ctx_obj.get('ledger')
    if path:
        return path
    files = glob.glob('*_ledger.db')
    if not files:
        files = glob.glob('*.se')
    if len(files) == 1:
        return files[0]
    if len(files) > 1:
        click.echo('Multiple ledger files found. Specify one with --ledger:', err=True)
        for f in sorted(files):
            click.echo(f'  {f}', err=True)
    else:
        click.echo('No ledger file found in current directory.', err=True)
        click.echo('Use: bokforing init --from-sie <previous_ledger.db> <year>', err=True)
    sys.exit(1)


@click.group()
@click.option('--ledger', '-l', default=None, metavar='FILE',
              help='Ledger DB or .se path (auto-detected if not set)')
@click.pass_context
def cli(ctx, ledger):
    """Bokforing — CLI double-entry accounting."""
    ctx.ensure_object(dict)
    ctx.obj['ledger'] = ledger


# ─── init ────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument('year', type=int)
@click.option('--from-sie', '-f', required=True, metavar='FILE',
              help='Previous year ledger (_ledger.db or .se) to carry balances from')
@click.option('--output', '-o', default=None, metavar='FILE',
              help='Output path stem (default: ledger_YYYY.se → creates ledger_YYYY_ledger.db)')
def init(year, from_sie, output):
    """Create a new ledger year from a previous year's closing balances.

    Example: bokforing init 2024 --from-sie ledger_2023_ledger.db
    """
    if not os.path.exists(from_sie):
        click.echo(f'Error: {from_sie} not found', err=True)
        sys.exit(1)

    prev = store_module.open_ledger(from_sie)
    new_sie, source = init_from_previous(prev, f'{year}0101', f'{year}1231')

    if output is None:
        output = f'ledger_{year}.se'

    db_out = store_module.db_path(output)
    if os.path.exists(db_out):
        if not click.confirm(f'{os.path.basename(db_out)} already exists. Overwrite?',
                             default=False):
            click.echo('Aborted.')
            return

    store_module.save_ledger(output, new_sie)

    # Summarise the opening balance sheet
    assets = sum(v for k, v in new_sie.ib.items()
                 if k.isdigit() and int(k) < 2000)
    equity = sum(v for k, v in new_sie.ib.items()
                 if k.isdigit() and 2000 <= int(k) < 3000)
    diff = assets + equity

    click.echo(f'Created {db_out}')
    click.echo(f'  Company : {new_sie.company_name}  ({new_sie.org_nr})')
    click.echo(f'  Period  : {year}-01-01 – {year}-12-31')
    click.echo(f'  Source  : {from_sie}  ({source})')
    click.echo(f'  Accounts: {len(new_sie.accounts)}  |  IB entries: {len(new_sie.ib)}')
    click.echo(f'')
    click.echo(f'  {"Assets (1xxx)":<30} {assets:>14,.2f}')
    click.echo(f'  {"Equity/liabilities (2xxx)":<30} {equity:>14,.2f}')
    color = 'green' if diff == 0 else 'red'
    label = 'Balanced ✓' if diff == 0 else f'Difference: {diff:+,.2f}  (!)'
    click.echo(click.style(f'  {label}', fg=color))


# ─── add ─────────────────────────────────────────────────────────────────────

@cli.command()
@click.pass_context
def add(ctx):
    """Add a new voucher interactively."""
    path = _resolve_ledger(ctx.obj)
    sie = store_module.open_ledger(path)
    account_map = sie.account_map()

    click.echo(f'\nAdding voucher to {os.path.basename(store_module.db_path(path))}')

    vdate = click.prompt('Date (YYYYMMDD)', default=_today())
    label = click.prompt('Description')

    transactions: list[Transaction] = []
    running = Decimal('0')

    click.echo('\nTransactions — enter account number or name, empty line when done:')
    while True:
        if transactions:
            color = 'green' if running == 0 else 'yellow'
            click.echo(click.style(f'  Running balance: {running:+.2f}', fg=color))

        acct_in = click.prompt('  Account', default='', show_default=False).strip()
        if not acct_in:
            break

        acc = find_account(sie, acct_in)
        if acc:
            click.echo(f'         → {acc.number}  {acc.label}')
            acct_nr = acc.number
        else:
            click.echo(f'         (account {acct_in} not in chart of accounts)')
            acct_nr = acct_in

        while True:
            raw = click.prompt('  Amount').strip().replace(' ', '').replace(',', '.')
            try:
                amount = Decimal(raw)
                break
            except InvalidOperation:
                click.echo('  Invalid amount, try again.')

        t_label = click.prompt('  Label', default='', show_default=False)

        transactions.append(Transaction(account=acct_nr, amount=amount,
                                        date=vdate, label=t_label))
        running += amount

    if not transactions:
        click.echo('No transactions entered — aborted.')
        return

    if running != 0:
        click.echo(click.style(f'\nVoucher does not balance (off by {running:+.2f})', fg='red'))
        if not click.confirm('Save unbalanced voucher?', default=False):
            click.echo('Aborted.')
            return
    else:
        click.echo(click.style('  Running balance: +0.00 ✓', fg='green'))

    click.echo(f'\n{"─" * 58}')
    click.echo(f'  {_fmt_date(vdate)}  {label}')
    for t in transactions:
        name = _acc_name(account_map, t.account)
        desc = t.label if t.label else name
        click.echo(f'  {t.account:<6}  {t.amount:>12.2f}  {desc}')
    click.echo(f'{"─" * 58}')

    if not click.confirm('\nSave?', default=True):
        click.echo('Aborted.')
        return

    num = next_voucher_number(sie)
    voucher = Voucher(series='A', number=num, date=vdate, label=label,
                      reg_date=_today(), signature='', transactions=transactions)
    sie.vouchers.append(voucher)
    store_module.save_ledger(path, sie)
    click.echo(f'Saved as A:{num} in {os.path.basename(store_module.db_path(path))}')


# ─── balance ─────────────────────────────────────────────────────────────────

@cli.command()
@click.argument('filter', required=False, default=None, metavar='[PREFIX]')
@click.pass_context
def balance(ctx, filter):
    """Show current account balances.

    Optionally filter by account number prefix, e.g. 'balance 1' for assets.
    """
    path = _resolve_ledger(ctx.obj)
    sie = store_module.open_ledger(path)
    balances = get_balances(sie)
    account_map = sie.account_map()

    accounts = sorted(balances.keys())
    if filter:
        accounts = [a for a in accounts if a.startswith(filter)]

    click.echo(f'\nBalances — {sie.company_name}')
    click.echo(f'{_fmt_date(sie.year_begins)} – {_fmt_date(sie.year_ends)}')
    click.echo('─' * 62)
    click.echo(f'  {"Acct":<6}  {"Description":<36}  {"Balance":>14}')
    click.echo('─' * 62)

    total_assets = Decimal('0')
    total_liab   = Decimal('0')
    total_pl     = Decimal('0')

    for acct in accounts:
        bal = balances[acct]
        name = _acc_name(account_map, acct)
        click.echo(f'  {acct:<6}  {name:<36}  {_fmt_amount(bal)}')
        if acct.startswith('1'):
            total_assets += bal
        elif acct.startswith('2'):
            total_liab += bal
        elif acct.isdigit() and 3000 <= int(acct) <= 8999:
            total_pl += bal

    click.echo('─' * 62)
    if not filter:
        click.echo(f'  {"Assets (1xxx)":<44}  {_fmt_amount(total_assets)}')
        click.echo(f'  {"Liabilities/equity (2xxx)":<44}  {_fmt_amount(total_liab)}')
        if total_pl != 0:
            click.echo(f'  {"Year-to-date P&L (3-8xxx, not yet closed)":<44}  {_fmt_amount(total_pl)}')
        net = total_assets + total_liab + total_pl
        color = 'green' if net == 0 else 'red'
        label = 'Balanced ✓' if net == 0 else 'Difference (!)'
        click.echo(click.style(f'  {label:<44}  {_fmt_amount(net)}', fg=color))
    click.echo()


# ─── list ─────────────────────────────────────────────────────────────────────

@cli.command('list')
@click.option('-n', default=20, show_default=True, help='Number of most recent vouchers')
@click.option('--all', 'show_all', is_flag=True, help='Show all vouchers')
@click.pass_context
def list_vouchers(ctx, n, show_all):
    """List vouchers."""
    path = _resolve_ledger(ctx.obj)
    sie = store_module.open_ledger(path)

    vouchers = sie.vouchers if show_all else sie.vouchers[-n:]
    total = len(sie.vouchers)

    with_docs = {(r['series'], r['number'])
                 for r in underlag_module.list_all(path)}

    click.echo(f'\nVouchers — {path}  ({total} total)')
    click.echo(f'  {"Ref":<7}  {"Date":10}  {"Description":<36}  {"Debit":>12}  U')
    click.echo('  ' + '─' * 75)
    for v in vouchers:
        debit = sum(t.amount for t in v.transactions if t.amount > 0)
        flag = '*' if (v.series, v.number) in with_docs else ' '
        click.echo(f'  {v.series}:{v.number:<5}  {_fmt_date(v.date):10}  {v.label:<36}  {debit:>12,.2f}  {flag}')
    if not show_all and total > n:
        click.echo(f'  … {total - n} earlier vouchers hidden (use --all to show)')
    click.echo()


# ─── show ─────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument('ref')
@click.pass_context
def show(ctx, ref):
    """Show voucher details. REF format: A:5 or just 5 (defaults to series A)."""
    path = _resolve_ledger(ctx.obj)
    sie = store_module.open_ledger(path)
    account_map = sie.account_map()

    series, num_str = (ref.split(':', 1) if ':' in ref else ('A', ref))
    if not num_str.isdigit():
        click.echo(f'Invalid reference: {ref}  (expected e.g. A:5 or 5)', err=True)
        sys.exit(1)
    num = int(num_str)

    v = next((x for x in sie.vouchers if x.series == series and x.number == num), None)
    if v is None:
        click.echo(f'Voucher {series}:{num} not found.', err=True)
        sys.exit(1)

    click.echo(f'\n{series}:{num}  {_fmt_date(v.date)}  {v.label}')
    click.echo(f'Registered: {_fmt_date(v.reg_date)}   Signature: {v.signature or "—"}')
    click.echo('─' * 58)
    for t in v.transactions:
        name = _acc_name(account_map, t.account)
        extra = f'  ({t.label})' if t.label else ''
        click.echo(f'  {t.account:<6}  {t.amount:>12.2f}  {name}{extra}')
    click.echo('─' * 58)
    total = v.total()
    color = 'green' if total == 0 else 'red'
    click.echo(click.style(f'  {"Total":>20}  {total:>12.2f}', fg=color))
    click.echo()


# ─── delete ──────────────────────────────────────────────────────────────────

@cli.command('delete')
@click.argument('ref')
@click.option('--dry-run', is_flag=True,
              help='Show what would change without writing anything.')
@click.pass_context
def delete_voucher_cmd(ctx, ref, dry_run):
    """Delete a voucher and renumber subsequent vouchers in the same series.

    REF format: A:5 or just 5 (defaults to series A).

    All underlag attached to the deleted voucher is also removed.
    Vouchers with higher numbers in the same series are shifted down by one
    and their underlag files renamed to match, using the same two-pass strategy
    as sort.  Label references to renumbered vouchers can be updated automatically.
    """
    path = _resolve_ledger(ctx.obj)
    sie = store_module.open_ledger(path)
    account_map = sie.account_map()

    series, num_str = (ref.split(':', 1) if ':' in ref else ('A', ref))
    if not num_str.isdigit():
        click.echo(f'Invalid reference: {ref}  (expected e.g. A:5 or 5)', err=True)
        sys.exit(1)
    num = int(num_str)

    v = next((x for x in sie.vouchers if x.series == series and x.number == num), None)
    if v is None:
        click.echo(f'Voucher {series}:{num} not found.', err=True)
        sys.exit(1)

    # Show the voucher to be deleted
    click.echo(f'\n{series}:{num}  {_fmt_date(v.date)}  {v.label}')
    click.echo(f'Registered: {_fmt_date(v.reg_date)}   Signature: {v.signature or "—"}')
    click.echo('─' * 58)
    for t in v.transactions:
        name = _acc_name(account_map, t.account)
        extra = f'  ({t.label})' if t.label else ''
        click.echo(f'  {t.account:<6}  {t.amount:>12.2f}  {name}{extra}')
    click.echo('─' * 58)

    underlag_files = underlag_module.list_for_voucher(path, series, num)
    if underlag_files:
        click.echo(f'  Underlag: {len(underlag_files)} file(s) attached')

    # Compute what changes after deletion
    new_sie, renumber_map = _delete_voucher_logic(sie, series, num)

    if renumber_map:
        click.echo(f'\n{len(renumber_map)} voucher(s) will be renumbered:')
        click.echo(f'  {"Old":<8}  {"New":<8}  Description')
        click.echo('  ' + '─' * 52)
        for (old_s, old_n), (new_s, new_n) in sorted(renumber_map.items()):
            v2 = next((x for x in sie.vouchers
                       if x.series == old_s and x.number == old_n), None)
            lbl = v2.label[:38] if v2 else ''
            click.echo(f'  {old_s}:{old_n:<6}  {new_s}:{new_n:<6}  {lbl}')

    # Label references to renumbered vouchers
    ref_changes = _collect_label_ref_changes(new_sie.vouchers, renumber_map)
    if ref_changes:
        click.echo(f'\n{len(ref_changes)} label(s) reference renumbered voucher(s):')
        click.echo(f'  {"Voucher":<8}  {"Field":<5}  '
                   f'{"Old label":<35}  →  New label')
        click.echo('  ' + '─' * 76)
        for v2, kind, _obj, old, new in ref_changes:
            field = 'label' if kind == 'label' else 'trans'
            click.echo(f'  {v2.series}:{v2.number:<5}  {field:<5}  {old:<35}  →  {new}')

    # Label references to the deleted voucher (will become dangling)
    dangling = []
    for v2 in new_sie.vouchers:
        if any(m.group(1) == series and int(m.group(2)) == num
               for m in _VER_REF_PAT.finditer(v2.label)):
            dangling.append((v2, 'label', v2.label))
        for t in v2.transactions:
            if t.label and any(m.group(1) == series and int(m.group(2)) == num
                               for m in _VER_REF_PAT.finditer(t.label)):
                dangling.append((v2, 'trans', t.label))
    if dangling:
        click.echo(click.style(
            f'\nWarning: {len(dangling)} label(s) reference the deleted '
            f'{series}:{num} and will become dangling:', fg='yellow'))
        for v2, kind, lbl in dangling:
            field = 'label' if kind == 'label' else 'trans'
            click.echo(f'  {v2.series}:{v2.number:<5}  {field:<5}  {lbl}')

    if dry_run:
        click.echo('\nDry run — no changes written.')
        return

    click.echo()
    update_refs = False
    if ref_changes:
        update_refs = click.confirm(
            f'Update {len(ref_changes)} label reference(s)?', default=True)

    underlag_info = (f', remove {len(underlag_files)} underlag file(s)'
                     if underlag_files else '')
    renumber_info = (f', renumber {len(renumber_map)} following voucher(s)'
                     if renumber_map else '')
    ref_info = (f', update {len(ref_changes)} label reference(s)'
                if update_refs else '')
    if not click.confirm(
            f'Delete {series}:{num}{underlag_info}{renumber_info}{ref_info}?',
            default=False):
        click.echo('Aborted.')
        return

    if update_refs:
        for _v, kind, obj, _old, new in ref_changes:
            obj.label = new

    n_underlag = underlag_module.remove_all_for_voucher(path, series, num)
    store_module.save_ledger(path, new_sie)
    n_files = underlag_module.renumber_vouchers(path, renumber_map)

    click.echo('Done.')
    click.echo(f'  Deleted {series}:{num}')
    if n_underlag:
        click.echo(f'  {n_underlag} underlag file(s) removed')
    if renumber_map:
        click.echo(f'  {len(renumber_map)} voucher(s) renumbered')
    if n_files:
        click.echo(f'  {n_files} underlag file(s) renamed')
    if update_refs:
        click.echo(f'  {len(ref_changes)} label reference(s) updated')


# ─── history ─────────────────────────────────────────────────────────────────

@cli.command()
@click.argument('account')
@click.pass_context
def history(ctx, account):
    """Show transaction history and running balance for an account."""
    path = _resolve_ledger(ctx.obj)
    sie = store_module.open_ledger(path)

    acc = find_account(sie, account)
    acct_nr = acc.number if acc else account
    title = f'{acc.number} — {acc.label}' if acc else account
    click.echo(f'\nHistory: {title}')

    rows = get_account_history(sie, acct_nr)
    running = sie.ib.get(acct_nr, Decimal('0'))

    click.echo(f'  {"Date":10}  {"Ref":<8}  {"Description":<28}  {"Amount":>12}  {"Balance":>12}')
    click.echo('  ' + '─' * 76)

    if running != 0:
        click.echo(f'  {"IB (opening balance)":>50}  {running:>12,.2f}')

    for v, t in rows:
        running += t.amount
        desc = (t.label if t.label else v.label)[:28]
        click.echo(f'  {_fmt_date(v.date):10}  {v.series}:{v.number:<6}  {desc:<28}  '
                   f'{t.amount:>12,.2f}  {running:>12,.2f}')

    if not rows:
        click.echo('  No transactions found.')
    click.echo()


# ─── verify ──────────────────────────────────────────────────────────────────

@cli.command()
@click.pass_context
def verify(ctx):
    """Verify that all vouchers balance (transactions sum to zero)."""
    path = _resolve_ledger(ctx.obj)
    sie = store_module.open_ledger(path)

    errors = [(v, v.total()) for v in sie.vouchers if v.total() != 0]

    if errors:
        click.echo(click.style(f'{len(errors)} unbalanced voucher(s) in {path}:', fg='red'))
        for v, total in errors:
            click.echo(f'  {v.series}:{v.number:<4}  {_fmt_date(v.date)}  '
                       f'{v.label:<35}  off by {total:+.2f}')
        sys.exit(1)
    else:
        click.echo(click.style(
            f'All {len(sie.vouchers)} vouchers in {path} balance. ✓', fg='green'))


# ─── scan ────────────────────────────────────────────────────────────────────

def _display_suggestion(suggestion: dict, account_map: dict, sie) -> None:
    """Print the AI suggestion in a readable format."""
    from decimal import Decimal as D
    conf_color = {'high': 'green', 'medium': 'yellow', 'low': 'red'}.get(
        suggestion.get('confidence', 'low'), 'white')

    click.echo(f'\n{"─" * 60}')
    click.echo(f'  Date:         {_fmt_date(suggestion.get("date", ""))}')
    click.echo(f'  Description:  {suggestion.get("description", "")}')
    click.echo()

    txns = suggestion.get('transactions', [])
    total = sum(t.get('amount', D('0')) for t in txns)
    for t in txns:
        name = account_map.get(t['account'], type('', (), {'label': ''})()).label
        lbl  = f'  ({t["label"]})' if t.get('label') else ''
        click.echo(f'  {t["account"]}  {t["amount"]:>12.2f}  {name}{lbl}')

    click.echo(f'  {"─" * 42}')
    bal_color = 'green' if total == 0 else 'red'
    click.echo(click.style(f'  Balance: {total:.2f}', fg=bal_color) +
               (' ✓' if total == 0 else '  (!)')   )

    if suggestion.get('notes'):
        click.echo()
        click.echo(f'  Notes: {suggestion["notes"]}')

    click.echo(
        click.style(
            f'\n  Confidence: {suggestion.get("confidence", "?")}',
            fg=conf_color,
        )
    )
    click.echo(f'{"─" * 60}')


def _edit_suggestion(suggestion: dict, sie, vdate: str, label: str,
                     transactions: list) -> tuple[str, str, list]:
    """Interactive editor pre-filled with the AI suggestion."""
    from decimal import Decimal, InvalidOperation as _IE

    click.echo('\nEdit voucher (press Enter to accept suggestion):')

    new_date = click.prompt('Date (YYYYMMDD)', default=vdate)
    new_label = click.prompt('Description', default=label)

    new_txns = []
    running = Decimal('0')

    click.echo('Transactions (empty account to finish, then add lines if needed):')
    # Pre-fill with suggested lines
    for t in transactions:
        acc_in = click.prompt(
            f'  Account', default=t['account'], show_default=True)
        if not acc_in.strip():
            break
        acc = find_account(sie, acc_in) or type('', (), {'number': acc_in, 'label': ''})()
        acct_nr = acc.number if hasattr(acc, 'number') else acc_in
        while True:
            raw = click.prompt('  Amount',
                               default=f'{t["amount"]:.2f}',
                               show_default=True).replace(',', '.')
            try:
                amount = Decimal(raw); break
            except _IE:
                click.echo('  Invalid amount.')
        lbl = click.prompt('  Label', default=t.get('label', ''),
                           show_default=bool(t.get('label')))
        new_txns.append({'account': acct_nr, 'amount': amount, 'label': lbl})
        running += amount
        color = 'green' if running == 0 else 'yellow'
        click.echo(click.style(f'  Running balance: {running:+.2f}', fg=color))

    # Allow adding extra lines
    while True:
        if running == 0:
            break
        click.echo(click.style(f'  Running balance: {running:+.2f}', fg='yellow'))
        acc_in = click.prompt('  Account (empty to finish)', default='',
                              show_default=False).strip()
        if not acc_in:
            break
        acc = find_account(sie, acc_in) or type('', (), {'number': acc_in, 'label': ''})()
        acct_nr = acc.number if hasattr(acc, 'number') else acc_in
        while True:
            raw = click.prompt('  Amount').replace(',', '.')
            try:
                amount = Decimal(raw); break
            except _IE:
                click.echo('  Invalid amount.')
        lbl = click.prompt('  Label', default='', show_default=False)
        new_txns.append({'account': acct_nr, 'amount': amount, 'label': lbl})
        running += amount

    return new_date, new_label, new_txns


@cli.command('scan')
@click.argument('file', type=click.Path(exists=True))
@click.option('--attach/--no-attach', default=True, show_default=True,
              help='Attach the file as underlag after saving the voucher.')
@click.option('--series', default='A', show_default=True,
              help='Voucher series to use.')
@click.pass_context
def scan(ctx, file, attach, series):
    """Analyse a receipt or invoice with AI and create a voucher.

    Sends the file to Claude, which reads the document and suggests date,
    description, and double-entry transactions.  The suggestion is displayed
    for review; you must explicitly accept or edit it before anything is saved.

    Requires ANTHROPIC_API_KEY to be set in the environment.
    """
    from .ai import suggest_voucher as _suggest
    from decimal import Decimal

    path = ctx.obj.get('ledger') if ctx.obj else None
    path = path or _resolve_ledger(ctx.obj)
    sie  = store_module.open_ledger(path)
    account_map = sie.account_map()

    samples = samples_module.list_samples(path)

    click.echo(f'Analysing {os.path.basename(file)} …')
    try:
        suggestion = _suggest(file, sie, samples=samples)
    except EnvironmentError as e:
        click.echo(click.style(str(e), fg='red'), err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(click.style(f'Analysis failed: {e}', fg='red'), err=True)
        sys.exit(1)

    vdate  = suggestion.get('date', _today())
    label  = suggestion.get('description', '')
    txns   = suggestion.get('transactions', [])

    _display_suggestion(suggestion, account_map, sie)

    # ── Sign-off loop ────────────────────────────────────────────────────
    while True:
        click.echo()
        choice = click.prompt(
            '[a]ccept  [e]dit  [d]iscard',
            default='a',
            prompt_suffix='  Choice',
        ).strip().lower()

        if choice == 'd':
            click.echo('Discarded.')
            return

        if choice == 'e':
            vdate, label, txns = _edit_suggestion(
                suggestion, sie, vdate, label, txns)
            # Rebuild display after editing
            suggestion = {**suggestion,
                          'date': vdate,
                          'description': label,
                          'transactions': txns,
                          'confidence': suggestion.get('confidence', '?')}
            _display_suggestion(suggestion, account_map, sie)
            continue

        if choice == 'a':
            total = sum(t.get('amount', Decimal('0')) for t in txns)
            if total != 0:
                click.echo(click.style(
                    f'Voucher does not balance (off by {total:+.2f}). '
                    'Edit before accepting.', fg='red'))
                continue
            break

        click.echo("Please enter 'a', 'e', or 'd'.")

    # ── Save ─────────────────────────────────────────────────────────────
    from .models import Voucher, Transaction
    num = next_voucher_number(sie, series)
    voucher = Voucher(
        series=series,
        number=num,
        date=vdate,
        label=label,
        reg_date=_today(),
        signature='',
        transactions=[
            Transaction(
                account=t['account'],
                amount=t['amount'],
                date=vdate,
                label=t.get('label', ''),
            )
            for t in txns
        ],
    )
    sie.vouchers.append(voucher)
    store_module.save_ledger(path, sie)
    click.echo(f'Saved as {series}:{num} in {os.path.basename(store_module.db_path(path))}')

    if attach:
        stored = underlag_module.add_file(path, series, num, file)
        click.echo(f'Underlag attached: {stored}')


# ─── skattekonto ─────────────────────────────────────────────────────────────

@cli.command('skattekonto')
@click.argument('csv_file', type=click.Path(exists=True))
@click.option('--from', 'from_date', default=None, metavar='YYYY-MM-DD',
              help='Start of date range (inclusive).')
@click.option('--to',   'to_date',   default=None, metavar='YYYY-MM-DD',
              help='End of date range (inclusive).')
@click.option('--series', default='A', show_default=True,
              help='Voucher series to use.')
@click.pass_context
def skattekonto_cmd(ctx, csv_file, from_date, to_date, series):
    """Import skattekonto transactions and create vouchers with AI suggestions.

    Reads a Skatteverket skattekonto CSV export, sends all transactions in
    the date range to Claude in a single call, then steps through each
    suggestion for operator sign-off.

    Sign-off options per transaction:
      a — accept and save
      e — edit before saving
      s — skip (do not save, continue to next)
      q — quit (stop processing, keep what was saved so far)

    Requires ANTHROPIC_API_KEY to be set in the environment.
    """
    from .skattekonto import parse_csv, suggest_vouchers as _suggest_batch
    from decimal import Decimal

    path = _resolve_ledger(ctx.obj)
    sie  = store_module.open_ledger(path)
    account_map = sie.account_map()

    # ── Parse CSV ─────────────────────────────────────────────────────────
    opening_balance, transactions = parse_csv(csv_file, from_date, to_date)

    if not transactions:
        click.echo('No transactions found in the specified date range.')
        return

    date_info = ''
    if from_date or to_date:
        date_info = f'  ({from_date or "…"} → {to_date or "…"})'
    click.echo(f'\nSkattekonto transactions to process{date_info}:')
    click.echo(f'  Opening balance in CSV: {opening_balance:,.2f}')
    click.echo()
    click.echo(f'  {"#":<4}  {"Date":10}  {"Amount":>10}  Description')
    click.echo('  ' + '─' * 55)
    for i, t in enumerate(transactions):
        click.echo(f'  {i:<4}  {_fmt_date(t["date"]):10}  '
                   f'{t["amount"]:>10,.2f}  {t["description"]}')
    click.echo()

    if not click.confirm(f'Send {len(transactions)} transaction(s) to Claude for analysis?',
                         default=True):
        click.echo('Aborted.')
        return

    samples = samples_module.list_samples(path)

    # ── AI batch call ─────────────────────────────────────────────────────
    click.echo('Analysing with Claude…')
    try:
        suggestions = _suggest_batch(transactions, sie, opening_balance,
                                     samples=samples)
    except EnvironmentError as e:
        click.echo(click.style(str(e), fg='red'), err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(click.style(f'Analysis failed: {e}', fg='red'), err=True)
        sys.exit(1)

    # Build a row_index → suggestion map for reliable lookup
    suggestion_map = {s.get('row_index', i): s
                      for i, s in enumerate(suggestions)}

    # ── Per-transaction sign-off ──────────────────────────────────────────
    saved = 0
    skipped = 0

    for i, txn in enumerate(transactions):
        suggestion = suggestion_map.get(i, {})
        if not suggestion:
            click.echo(f'\n[{i+1}/{len(transactions)}] No suggestion returned for '
                       f'{txn["date_display"]} {txn["description"]} — skipping.')
            skipped += 1
            continue

        vdate = suggestion.get('date', txn['date'])
        label = suggestion.get('description', txn['description'])
        txns  = suggestion.get('transactions', [])

        click.echo(f'\n[{i+1}/{len(transactions)}] '
                   f'{txn["date_display"]}  {txn["description"]}  '
                   f'({txn["amount"]:+,.2f})')
        _display_suggestion(suggestion, account_map, sie)

        # Sign-off loop
        while True:
            click.echo()
            choice = click.prompt(
                '[a]ccept  [e]dit  [s]kip  [q]uit',
                default='a',
                prompt_suffix='  Choice',
            ).strip().lower()

            if choice == 'q':
                click.echo(f'\nStopped.  Saved {saved}, skipped {skipped + (len(transactions) - i - 1)} remaining.')
                return

            if choice == 's':
                skipped += 1
                break

            if choice == 'e':
                vdate, label, txns = _edit_suggestion(
                    suggestion, sie, vdate, label, txns)
                suggestion = {**suggestion, 'date': vdate,
                              'description': label, 'transactions': txns}
                _display_suggestion(suggestion, account_map, sie)
                continue

            if choice == 'a':
                total = sum(t.get('amount', Decimal('0')) for t in txns)
                if total != 0:
                    click.echo(click.style(
                        f'Voucher does not balance (off by {total:+.2f}). '
                        'Edit before accepting.', fg='red'))
                    continue
                break

            click.echo("Please enter 'a', 'e', 's', or 'q'.")

        if choice == 's':
            continue

        # Save
        from .models import Voucher, Transaction
        num = next_voucher_number(sie, series)
        voucher = Voucher(
            series=series, number=num,
            date=vdate, label=label,
            reg_date=_today(), signature='',
            transactions=[
                Transaction(account=t['account'], amount=t['amount'],
                            date=vdate, label=t.get('label', ''))
                for t in txns
            ],
        )
        sie.vouchers.append(voucher)
        store_module.save_ledger(path, sie)
        sie = store_module.open_ledger(path)
        saved += 1
        click.echo(f'Saved as {series}:{num}')

    click.echo(f'\nDone — {saved} voucher(s) saved, {skipped} skipped.')


# ─── sort ────────────────────────────────────────────────────────────────────

_VER_REF_PAT = re.compile(r'([A-Z]):(\d+)')


def _apply_ref_subst(text: str, renumber_map: dict) -> str:
    def repl(m: re.Match) -> str:
        key = (m.group(1), int(m.group(2)))
        if key in renumber_map:
            new_s, new_n = renumber_map[key]
            return f'{new_s}:{new_n}'
        return m.group(0)
    return _VER_REF_PAT.sub(repl, text)


def _collect_label_ref_changes(vouchers: list, renumber_map: dict) -> list:
    """Return list of (voucher, kind, obj, old_text, new_text).

    kind is 'label' (voucher.label) or 'trans' (transaction.label).
    obj is the voucher or transaction whose label field needs updating.
    """
    changes = []
    for v in vouchers:
        new_lbl = _apply_ref_subst(v.label, renumber_map)
        if new_lbl != v.label:
            changes.append((v, 'label', v, v.label, new_lbl))
        for t in v.transactions:
            if t.label:
                new_tlbl = _apply_ref_subst(t.label, renumber_map)
                if new_tlbl != t.label:
                    changes.append((v, 'trans', t, t.label, new_tlbl))
    return changes


@cli.command('sort')
@click.option('--by', 'sort_by',
              type=click.Choice(['registration-date', 'voucher-date'],
                                case_sensitive=False),
              default='registration-date', show_default=True,
              help='Date field to sort by within each series.')
@click.option('--dry-run', is_flag=True,
              help='Show what would change without writing anything.')
@click.pass_context
def sort_cmd(ctx, sort_by, dry_run):
    """Sort and renumber vouchers within each series, rename underlag files.

    Vouchers are sorted by the chosen date and renumbered 1, 2, 3, …
    Underlag files are renamed to match the new numbers using a
    collision-safe two-pass rename.  The ledger file is rewritten in full.

    This is a one-time sanitise operation; normal voucher entry is
    append-only and never renumbers existing vouchers.
    """
    path = _resolve_ledger(ctx.obj)
    sie = store_module.open_ledger(path)

    key = 'reg_date' if sort_by == 'registration-date' else 'date'
    new_sie, renumber_map = sort_vouchers(sie, key=key)

    if not renumber_map:
        click.echo('Vouchers are already in order — nothing to do.')
        return

    click.echo(f'Sort by {sort_by} — {len(renumber_map)} voucher(s) will be renumbered:\n')
    click.echo(f'  {"Old":<8}  {"New":<8}  {"Reg date":10}  {"Voucher date":12}  Description')
    click.echo('  ' + '─' * 72)
    for (old_s, old_n), (new_s, new_n) in sorted(renumber_map.items()):
        v = next((v for v in sie.vouchers
                  if v.series == old_s and v.number == old_n), None)
        reg  = _fmt_date(v.reg_date)  if v and v.reg_date  else '—'
        vdat = _fmt_date(v.date)      if v and v.date       else '—'
        lbl  = (v.label[:35])         if v                  else ''
        click.echo(f'  {old_s}:{old_n:<6}  {new_s}:{new_n:<6}  {reg:10}  {vdat:12}  {lbl}')

    # Check for voucher references embedded in labels
    ref_changes = _collect_label_ref_changes(new_sie.vouchers, renumber_map)
    update_refs = False
    if ref_changes:
        click.echo(f'\n{len(ref_changes)} label(s) reference renumbered voucher(s):')
        click.echo(f'  {"Voucher":<8}  {"Field":<5}  '
                   f'{"Old label":<35}  →  New label')
        click.echo('  ' + '─' * 76)
        for v, kind, _obj, old, new in ref_changes:
            field = 'label' if kind == 'label' else 'trans'
            click.echo(f'  {v.series}:{v.number:<5}  {field:<5}  {old:<35}  →  {new}')

    if dry_run:
        click.echo('\nDry run — no changes written.')
        return

    click.echo()
    if ref_changes:
        update_refs = click.confirm(
            f'Update {len(ref_changes)} label reference(s)?', default=True)

    suffix = (f', update {len(ref_changes)} label reference(s)' if update_refs else '')
    if not click.confirm(
            f'Rewrite {os.path.basename(path)} and rename underlag{suffix}?',
            default=True):
        click.echo('Aborted.')
        return

    if update_refs:
        for _v, kind, obj, _old, new in ref_changes:
            if kind == 'label':
                obj.label = new
            else:
                obj.label = new

    store_module.save_ledger(path, new_sie)

    n_files = underlag_module.renumber_vouchers(path, renumber_map)

    click.echo('Done.')
    click.echo(f'  {len(renumber_map)} vouchers renumbered')
    if n_files:
        click.echo(f'  {n_files} underlag file(s) renamed')
    if update_refs:
        click.echo(f'  {len(ref_changes)} label reference(s) updated')


# ─── hash ────────────────────────────────────────────────────────────────────

@cli.command('hash')
@click.option('--dry-run', is_flag=True,
              help='Compute hashes but do not write them to the chain table.')
@click.option('--force', is_flag=True,
              help='Recompute and overwrite all existing chain entries.')
@click.pass_context
def hash_cmd(ctx, dry_run, force):
    """Bootstrap the cryptographic hash chain for IB and all vouchers.

    Computes SHA-256 hashes for the opening balance record (IB) and every
    voucher that does not yet have an entry in the chain table.  Entries
    already in the chain table are never recomputed — safe to re-run.

    Each voucher series gets its own independent chain rooted at the same IB
    hash.  The IB hash is computed first and shared across all series.

    Run this once on a ledger that predates the hash chain, or after importing
    old vouchers.  Does not contact a TSA server — use 'lock' afterwards for
    a trusted timestamp.

    Exits with status 1 if any series has a gap (missing voucher number).
    """
    import hashlib as _hashlib
    from collections import defaultdict

    path = _resolve_ledger(ctx.obj)
    sie = store_module.open_ledger(path)

    # ── IB root ───────────────────────────────────────────────────────────────
    ib_stored = store_module.get_chain_entry(path, 'IB', 0)
    if ib_stored and not force:
        ib_hash = ib_stored
        click.echo(f'IB  already hashed: {ib_hash[:16]}…')
    else:
        ib_text = canonical_ib_text(sie)
        ib_hash = _hashlib.sha256(ib_text.encode('utf-8')).hexdigest()
        if not dry_run:
            store_module.insert_chain_entry(path, 'IB', 0, ib_hash, replace=force)
        suffix = '  [dry-run]' if dry_run else ''
        click.echo(f'IB  → {ib_hash[:16]}…{suffix}')

    # ── All voucher series ────────────────────────────────────────────────────
    series_groups: dict[str, list] = defaultdict(list)
    for v in sie.vouchers:
        series_groups[v.series].append(v)

    if not series_groups:
        click.echo('No vouchers.')
        return

    total_new = 0
    total_already = 0

    for series_id in sorted(series_groups):
        vouchers = sorted(series_groups[series_id], key=lambda v: v.number)

        # Verify no gaps in this series
        for expected, v in enumerate(vouchers, start=1):
            if v.number != expected:
                click.echo(
                    click.style(
                        f'Gap in {series_id}-series: expected {series_id}:{expected},'
                        f' found {series_id}:{v.number}',
                        fg='red',
                    ),
                    err=True,
                )
                sys.exit(1)

        prev_hash = ib_hash
        new_count = 0
        already_count = 0

        for v in vouchers:
            existing = store_module.get_chain_entry(path, series_id, v.number)
            if existing and not force:
                prev_hash = existing
                already_count += 1
                continue

            u_hashes = underlag_module.get_underlag_hashes(path, v.series, v.number)
            v_text = canonical_voucher_text(v, prev_hash, u_hashes)
            v_hash = _hashlib.sha256(v_text.encode('utf-8')).hexdigest()

            if not dry_run:
                store_module.insert_chain_entry(
                    path, series_id, v.number, v_hash, replace=force)

            prev_hash = v_hash
            new_count += 1
            suffix = '  [dry-run]' if dry_run else ''
            click.echo(f'{series_id}:{v.number:<4}  → {v_hash[:16]}…{suffix}')

        if already_count and not new_count:
            click.echo(f'{series_id}   {already_count} already in chain')

        total_new += new_count
        total_already += already_count

    already_note = f', {total_already} already in chain' if total_already else ''
    dry_note = '  [dry-run — nothing written]' if dry_run else ''
    click.echo(f'\n{total_new} voucher(s) hashed{already_note}{dry_note}')


# ─── preimage ────────────────────────────────────────────────────────────────

@cli.command('preimage')
@click.argument('ref')
@click.pass_context
def preimage_cmd(ctx, ref):
    """Display the canonical preimage text for a voucher or the IB root.

    REF: 'IB' for the opening-balance root, 'A:5' or '5' for a voucher.

    The preimage is the exact UTF-8 text whose SHA-256 is stored in the chain
    table.  Piping the output to sha256sum lets you independently verify the
    stored hash:

        bokforing preimage A:5 | sha256sum
    """
    import hashlib as _hashlib

    path = _resolve_ledger(ctx.obj)
    sie = store_module.open_ledger(path)

    ref_upper = ref.strip().upper()

    # ── IB root ───────────────────────────────────────────────────────────────
    if ref_upper == 'IB':
        text = canonical_ib_text(sie)
        stored_hash = store_module.get_chain_entry(path, 'IB', 0)
        computed = _hashlib.sha256(text.encode('utf-8')).hexdigest()
        click.echo(text, nl=False)
        _print_hash_footer(computed, stored_hash)
        return

    # ── A-series voucher ──────────────────────────────────────────────────────
    series, num_str = (ref.split(':', 1) if ':' in ref else ('A', ref))
    if not num_str.isdigit():
        click.echo(f'Invalid reference: {ref}  (expected IB, A:5, or 5)', err=True)
        sys.exit(1)
    num = int(num_str)

    v = next((x for x in sie.vouchers if x.series == series and x.number == num), None)
    if v is None:
        click.echo(f'Voucher {series}:{num} not found.', err=True)
        sys.exit(1)

    # Resolve PREV hash — every series is rooted at IB for voucher number 1
    if num == 1:
        prev_hash = store_module.get_chain_entry(path, 'IB', 0)
        prev_label = 'IB'
    else:
        prev_hash = store_module.get_chain_entry(path, series, num - 1)
        prev_label = f'{series}:{num - 1}'

    if prev_hash is None:
        click.echo(
            f'Cannot show preimage: previous entry ({prev_label}) is not yet hashed.\n'
            f"Run 'bokforing hash' first.",
            err=True,
        )
        sys.exit(1)

    u_hashes = underlag_module.get_underlag_hashes(path, series, num)
    text = canonical_voucher_text(v, prev_hash, u_hashes)
    stored_hash = store_module.get_chain_entry(path, series, num)
    computed = _hashlib.sha256(text.encode('utf-8')).hexdigest()
    click.echo(text, nl=False)
    _print_hash_footer(computed, stored_hash)


def _print_hash_footer(computed: str, stored: str | None) -> None:
    if stored is None:
        status = click.style('not in chain — run hash first', fg='yellow')
    elif computed == stored:
        status = click.style('matches chain ✓', fg='green')
    else:
        status = click.style(f'MISMATCH — chain has {stored}', fg='red')
    click.echo(f'\nSHA-256: {computed}  [{status}]', err=True)


# ─── lock ────────────────────────────────────────────────────────────────────

@cli.command('lock')
@click.argument('date', required=False, default=None, metavar='DATE')
@click.option('--tsa-url', default='http://timestamp.digicert.com', show_default=True,
              help='RFC 3161 TSA endpoint URL.')
@click.pass_context
def lock_cmd(ctx, date, tsa_url):
    """Timestamp the hash chain tail of every series with an RFC 3161 timestamp.

    DATE (optional, YYYY-MM-DD): timestamp the last voucher in each series
    whose economic date is on or before DATE.  If omitted, the overall tail
    of each series is used.

    One TSA call is made per series.  The .tsr blob and UTC timestamp are
    stored in the chain table for each series tail.  Typical use at year-end:

        bokforing lock 2025-12-31

    Chain entries must already exist — run 'bokforing hash' first.
    """
    import sqlite3 as _sqlite3
    from collections import defaultdict
    from .tsa import request_timestamp

    path = _resolve_ledger(ctx.obj)
    sie = store_module.open_ledger(path)

    series_groups: dict[str, list] = defaultdict(list)
    for v in sie.vouchers:
        series_groups[v.series].append(v)

    if not series_groups:
        click.echo('No vouchers to lock.', err=True)
        sys.exit(1)

    cutoff = date.replace('-', '') if date else None

    # Resolve target voucher and chain hash per series
    targets = []   # (series_id, target_voucher, chain_hash)
    skipped = []
    for series_id in sorted(series_groups):
        vouchers = sorted(series_groups[series_id], key=lambda v: v.number)
        candidates = [v for v in vouchers if v.date <= cutoff] if cutoff else vouchers
        if not candidates:
            continue
        target = candidates[-1]
        chain_hash = store_module.get_chain_entry(path, series_id, target.number)
        if chain_hash is None:
            skipped.append(f'{series_id}:{target.number}')
            continue
        targets.append((series_id, target, chain_hash))

    if skipped:
        click.echo(
            click.style(
                'No chain entry for: ' + ', '.join(skipped) +
                ' — run "bokforing hash" first.',
                fg='yellow',
            )
        )
    if not targets:
        click.echo('Nothing to lock.')
        return

    # Warn about already-locked entries and ask once to confirm overwrite
    db_p = store_module.db_path(path)
    conn = _sqlite3.connect(db_p)
    try:
        already_locked = [
            (s, t, h) for s, t, h in targets
            if conn.execute(
                'SELECT tsa_timestamp FROM chain WHERE voucher_series=? AND voucher_number=?',
                (s, t.number),
            ).fetchone()[0]
        ]
    finally:
        conn.close()

    if already_locked:
        for s, t, _ in already_locked:
            click.echo(click.style(f'{s}:{t.number} is already locked', fg='yellow'))
        if not click.confirm(
                f'Overwrite {len(already_locked)} existing timestamp(s)?', default=False):
            click.echo('Aborted.')
            return

    click.echo(f'TSA: {tsa_url}\n')

    locked_count = 0
    for series_id, target, chain_hash in targets:
        label = f'{series_id}:{target.number:<4}  {_fmt_date(target.date)}  {chain_hash[:16]}…'
        click.echo(f'  {label} ', nl=False)
        try:
            tsr_bytes, iso_ts = request_timestamp(chain_hash, tsa_url=tsa_url)
        except RuntimeError as exc:
            click.echo(click.style(f'FAILED: {exc}', fg='red'))
            continue
        store_module.update_chain_tsr(path, series_id, target.number, tsr_bytes, iso_ts)
        click.echo(click.style(f'→  {iso_ts}  ✓', fg='green'))
        locked_count += 1

    click.echo(f'\n{locked_count} series locked.')


# ─── export ──────────────────────────────────────────────────────────────────

@cli.command('export')
@click.option('--output', '-o', default=None, metavar='FILE',
              help='Output .se file (default: ledger_YYYY.se alongside the DB)')
@click.pass_context
def export_cmd(ctx, output):
    """Export the ledger to a SIE 4 .se file with a companion _underlag/ directory.

    Writes a plain-text SIE 4 file and, if underlag is present, a _underlag/
    directory with all attached supporting documents using the Verifikation
    naming convention.  Useful for sharing with external tools or archiving.
    """
    path = _resolve_ledger(ctx.obj)
    db_p = store_module.db_path(path)

    if output is None:
        base = db_p[:-len('_ledger.db')]
        output = base + '.se'

    store_module.export_sie(path, output)

    base = os.path.splitext(os.path.abspath(output))[0]
    underlag_dir = base + '_underlag'
    n_files = len(os.listdir(underlag_dir)) if os.path.isdir(underlag_dir) else 0
    click.echo(f'Exported {output}')
    if n_files:
        click.echo(f'  {n_files} underlag file(s) → {os.path.basename(underlag_dir)}/')


# ─── report ──────────────────────────────────────────────────────────────────

@cli.command()
@click.option('--prev-sie', '-p', default=None, metavar='FILE',
              help='Previous year ledger for comparison column')
@click.option('--output', '-o', default=None, metavar='FILE',
              help='Output .ods file (default: Resultatrapport_YYYY-MM-DD-YYYY-MM-DD.ods)')
@click.pass_context
def report(ctx, prev_sie, output):
    """Generate a Resultatrapport (income statement) as a LibreOffice ODS file."""
    path = _resolve_ledger(ctx.obj)
    sie = store_module.open_ledger(path)

    prev = None
    if prev_sie:
        if not os.path.exists(prev_sie):
            click.echo(f'Error: {prev_sie} not found', err=True)
            sys.exit(1)
        prev = store_module.open_ledger(prev_sie)

    if output is None:
        b = f'{sie.year_begins[:4]}-{sie.year_begins[4:6]}-{sie.year_begins[6:]}'
        e = f'{sie.year_ends[:4]}-{sie.year_ends[4:6]}-{sie.year_ends[6:]}'
        output = os.path.join(os.path.dirname(os.path.abspath(path)),
                              f'Resultatrapport_{b}-{e}.ods')

    generate_resultatrapport(sie, prev, output)
    click.echo(f'Written {output}')
    if prev:
        click.echo(f'  Current year : {sie.year_begins} – {sie.year_ends}')
        click.echo(f'  Previous year: {prev.year_begins} – {prev.year_ends}')


@cli.command()
@click.option('--output', '-o', default=None, metavar='FILE',
              help='Output .ods file (default: Balansrapport_YYYY-MM-DD-YYYY-MM-DD.ods)')
@click.pass_context
def balansrapport(ctx, output):
    """Generate a Balansrapport (balance sheet) as a LibreOffice ODS file."""
    path = _resolve_ledger(ctx.obj)
    sie = store_module.open_ledger(path)

    if output is None:
        b = f'{sie.year_begins[:4]}-{sie.year_begins[4:6]}-{sie.year_begins[6:]}'
        e = f'{sie.year_ends[:4]}-{sie.year_ends[4:6]}-{sie.year_ends[6:]}'
        output = os.path.join(os.path.dirname(os.path.abspath(path)),
                              f'Balansrapport_{b}-{e}.ods')

    generate_balansrapport(sie, output)
    click.echo(f'Written {output}')


# ─── sie5export ──────────────────────────────────────────────────────────────

@cli.command('sie5export')
@click.option('--output', '-o', default=None, metavar='FILE',
              help='Output .si5 file (default: CompanyName_YYYY-MM-DD-YYYY-MM-DD.si5)')
@click.pass_context
def sie5export(ctx, output):
    """Export a SIE 5 package (.si5) combining the ledger with any attached underlag.

    The resulting file is a zip archive containing sie5.xml plus every
    underlag file linked to a voucher, referenced from the XML.
    """
    from .sie5 import generate_sie5

    path = _resolve_ledger(ctx.obj)
    sie  = store_module.open_ledger(path)

    if output is None:
        b     = f'{sie.year_begins[:4]}-{sie.year_begins[4:6]}-{sie.year_begins[6:]}'
        e     = f'{sie.year_ends[:4]}-{sie.year_ends[4:6]}-{sie.year_ends[6:]}'
        stem  = sie.company_name.replace(' ', '_').replace('/', '-')
        output = os.path.join(os.path.dirname(os.path.abspath(path)),
                              f'{stem}_{b}_{e}.si5')

    n_vouchers, n_docs = generate_sie5(sie, path, output)

    size_kb = os.path.getsize(output) / 1024
    click.echo(f'Written {output}  ({size_kb:.1f} KB)')
    click.echo(f'  {n_vouchers} vouchers,  {n_docs} attached documents')
    if n_docs == 0:
        click.echo('  (no underlag found — use "underlag add" to attach files)')


@cli.command('sie5import')
@click.argument('si5_file', type=click.Path(exists=True))
@click.option('--output', '-o', default=None, metavar='FILE',
              help='Output path stem (default: derived from company name and year)')
@click.pass_context
def sie5import(ctx, si5_file, output):
    """Restore a ledger year from a SIE 5 package (.si5).

    Creates a _ledger.db with all accounting data and embedded supporting
    documents from the package.

    Note: #SRU codes are not stored in SIE 5 and will not be present
    in the restored ledger.
    """
    from .sie5 import restore_from_sie5

    if output is None:
        # Peek at the XML to get company name and year before we do the full restore
        import zipfile, xml.etree.ElementTree as _ET
        _ns = {'s': 'http://www.sie.se/sie5'}
        with zipfile.ZipFile(si5_file) as _zf:
            _root = _ET.fromstring(_zf.read('sie5.xml'))
        _co  = _root.find('s:FileInfo/s:Company', _ns)
        _fy  = _root.find('s:FiscalYears/s:FiscalYear', _ns)
        _name = (_co.get('Name', 'ledger') if _co is not None else 'ledger')
        _yr   = (_fy.get('Start', '')[:4]  if _fy is not None else '')
        stem  = _name.replace(' ', '_').replace('/', '-')
        output = os.path.join(os.path.dirname(os.path.abspath(si5_file)),
                              f'{stem}_{_yr}.se' if _yr else f'{stem}.se')

    db_out = store_module.db_path(output)
    if os.path.exists(db_out):
        if not click.confirm(f'{os.path.basename(db_out)} already exists. Overwrite?',
                             default=False):
            click.echo('Aborted.')
            return

    sie, n_docs = restore_from_sie5(si5_file, output)

    click.echo(f'Restored {db_out}')
    click.echo(f'  Company : {sie.company_name}  ({sie.org_nr})')
    click.echo(f'  Period  : {_fmt_date(sie.year_begins)} – {_fmt_date(sie.year_ends)}')
    click.echo(f'  Accounts: {len(sie.accounts)}  |  Vouchers: {len(sie.vouchers)}')
    click.echo(f'  IB entries: {len(sie.ib)}  |  UB entries: {len(sie.ub)}')
    click.echo(f'  Underlag documents restored: {n_docs}')
    if not sie.accounts[0].sru if sie.accounts else True:
        click.echo('  Note: SRU codes are not stored in SIE 5 — not present in restored file')


# ─── sample ──────────────────────────────────────────────────────────────────

@cli.group('sample')
@click.pass_context
def sample_group(ctx):
    """Manage sample vouchers used as AI account-selection hints.

    Samples are stored in samples.json alongside the ledger and sent to
    Claude with every scan/skattekonto call to guide account selection.
    """
    pass


@sample_group.command('add')
@click.pass_context
def sample_add(ctx):
    """Add a sample voucher interactively."""
    from decimal import Decimal, InvalidOperation

    path = _resolve_ledger(ctx.obj)
    sie = store_module.open_ledger(path)
    account_map = sie.account_map()

    click.echo('\nAdding sample voucher')
    description = click.prompt('Description')
    notes = click.prompt('Notes (optional)', default='', show_default=False)

    transactions: list[dict] = []
    running = Decimal('0')

    click.echo('\nTransactions — enter account number or name, empty line when done:')
    while True:
        if transactions:
            color = 'green' if running == 0 else 'yellow'
            click.echo(click.style(f'  Running balance: {running:+.2f}', fg=color))

        acct_in = click.prompt('  Account', default='', show_default=False).strip()
        if not acct_in:
            break

        acc = find_account(sie, acct_in)
        if acc:
            click.echo(f'         → {acc.number}  {acc.label}')
            acct_nr = acc.number
        else:
            click.echo(f'         (account {acct_in} not in chart of accounts)')
            acct_nr = acct_in

        while True:
            raw = click.prompt('  Amount').strip().replace(' ', '').replace(',', '.')
            try:
                amount = Decimal(raw)
                break
            except InvalidOperation:
                click.echo('  Invalid amount, try again.')

        t_label = click.prompt('  Label', default='', show_default=False)
        transactions.append({'account': acct_nr, 'amount': str(amount),
                             'label': t_label})
        running += amount

    if not transactions:
        click.echo('No transactions entered — aborted.')
        return

    if running != 0:
        click.echo(click.style(
            f'\nSample does not balance (off by {running:+.2f})', fg='yellow'))
        if not click.confirm('Save unbalanced sample?', default=False):
            click.echo('Aborted.')
            return

    click.echo(f'\n{"─" * 58}')
    click.echo(f'  {description}')
    for t in transactions:
        name = _acc_name(account_map, t['account'])
        desc = t['label'] if t['label'] else name
        click.echo(f'  {t["account"]:<6}  {t["amount"]:>12}  {desc}')
    if notes:
        click.echo(f'  Notes: {notes}')
    click.echo(f'{"─" * 58}')

    if not click.confirm('\nSave?', default=True):
        click.echo('Aborted.')
        return

    sample = samples_module.add_sample(path, description, transactions, notes)
    click.echo(f'Saved as sample #{sample["id"]}')


@sample_group.command('list')
@click.pass_context
def sample_list(ctx):
    """List all sample vouchers."""
    path = _resolve_ledger(ctx.obj)
    all_samples = samples_module.list_samples(path)

    if not all_samples:
        click.echo('No sample vouchers defined.')
        click.echo('Use: bokforing sample add')
        return

    samples_path = samples_module._samples_path(path)
    click.echo(f'\nSample vouchers — {os.path.basename(samples_path)}  '
               f'({len(all_samples)} total)')
    click.echo(f'  {"#":>4}  {"Description":<45}  Txns')
    click.echo('  ' + '─' * 58)
    for s in all_samples:
        click.echo(f'  {s["id"]:>4}  {s["description"]:<45}  '
                   f'{len(s["transactions"])}')
    click.echo()


@sample_group.command('show')
@click.argument('sample_id', type=int)
@click.pass_context
def sample_show(ctx, sample_id):
    """Show details of a sample voucher. SAMPLE_ID is the numeric ID."""
    path = _resolve_ledger(ctx.obj)
    sie = store_module.open_ledger(path)
    account_map = sie.account_map()

    s = samples_module.get_sample(path, sample_id)
    if s is None:
        click.echo(f'Sample #{sample_id} not found.', err=True)
        sys.exit(1)

    click.echo(f'\nSample #{s["id"]}: {s["description"]}')
    click.echo('─' * 58)
    for t in s['transactions']:
        name = _acc_name(account_map, t['account'])
        lbl = f'  ({t["label"]})' if t.get('label') else ''
        click.echo(f'  {t["account"]:<6}  {t["amount"]:>12}  {name}{lbl}')
    click.echo('─' * 58)
    if s.get('notes'):
        click.echo(f'Notes: {s["notes"]}')
    click.echo()


@sample_group.command('from-voucher')
@click.argument('ref')
@click.pass_context
def sample_from_voucher(ctx, ref):
    """Add a sample by copying an existing voucher. REF format: A:5 or 5."""
    path = _resolve_ledger(ctx.obj)
    sie = store_module.open_ledger(path)
    account_map = sie.account_map()

    series, num_str = (ref.split(':', 1) if ':' in ref else ('A', ref))
    if not num_str.isdigit():
        click.echo(f'Invalid reference: {ref}  (expected e.g. A:5 or 5)', err=True)
        sys.exit(1)
    num = int(num_str)

    v = next((x for x in sie.vouchers if x.series == series and x.number == num), None)
    if v is None:
        click.echo(f'Voucher {series}:{num} not found.', err=True)
        sys.exit(1)

    click.echo(f'\n{series}:{num}  {_fmt_date(v.date)}  {v.label}')
    click.echo('─' * 58)
    for t in v.transactions:
        name = _acc_name(account_map, t.account)
        lbl = f'  ({t.label})' if t.label else ''
        click.echo(f'  {t.account:<6}  {t.amount:>12.2f}  {name}{lbl}')
    click.echo('─' * 58)

    description = click.prompt('\nDescription', default=v.label)
    notes = click.prompt('Notes (optional)', default='', show_default=False)

    if not click.confirm('Save as sample?', default=True):
        click.echo('Aborted.')
        return

    transactions = [
        {'account': t.account, 'amount': str(t.amount),
         **({'label': t.label} if t.label else {})}
        for t in v.transactions
    ]
    sample = samples_module.add_sample(path, description, transactions, notes)
    click.echo(f'Saved as sample #{sample["id"]}')


@sample_group.command('delete')
@click.argument('sample_id', type=int)
@click.pass_context
def sample_delete(ctx, sample_id):
    """Delete a sample voucher by ID."""
    path = _resolve_ledger(ctx.obj)
    s = samples_module.get_sample(path, sample_id)
    if s is None:
        click.echo(f'Sample #{sample_id} not found.', err=True)
        sys.exit(1)

    click.echo(f'  #{s["id"]}  {s["description"]}')
    if not click.confirm('Delete?', default=False):
        click.echo('Aborted.')
        return

    samples_module.delete_sample(path, sample_id)
    click.echo(f'Deleted sample #{sample_id}.')


# ─── accounts ────────────────────────────────────────────────────────────────

@cli.group('accounts')
@click.pass_context
def accounts_group(ctx):
    """Manage the chart of accounts.

    Accounts can be looked up against the BAS-kontoplan (Swedish standard
    chart of accounts) to get the canonical name and account type.
    """
    pass


@accounts_group.command('list')
@click.argument('prefix', required=False, default=None, metavar='[PREFIX]')
@click.pass_context
def accounts_list(ctx, prefix):
    """List accounts in the chart of accounts.

    Optionally filter by account number prefix, e.g. 'accounts list 3' for
    income accounts.
    """
    from . import bas as bas_module

    path = _resolve_ledger(ctx.obj)
    sie = store_module.open_ledger(path)

    accounts = sorted(sie.accounts, key=lambda a: a.number)
    if prefix:
        accounts = [a for a in accounts if a.number.startswith(prefix)]

    click.echo(f'\nChart of accounts — {sie.company_name}  ({len(sie.accounts)} total)')
    if prefix:
        click.echo(f'Filter: {prefix}*  ({len(accounts)} matching)')
    click.echo('─' * 62)
    click.echo(f'  {"Nr":<6}  {"Type"}  {"Description"}')
    click.echo('─' * 62)
    for acc in accounts:
        bas_entry = bas_module.lookup(acc.number)
        bas_marker = ' *' if bas_entry and bas_entry[0] != acc.label else ''
        click.echo(f'  {acc.number:<6}  {acc.ktyp or "?":^4}  {acc.label}{bas_marker}')
    click.echo()


@accounts_group.command('add')
@click.argument('number')
@click.pass_context
def accounts_add(ctx, number):
    """Add a new account to the chart of accounts.

    NUMBER is a 4-digit account number, e.g. 3105.  The BAS-kontoplan is
    consulted and the standard name is offered as a default.
    """
    import bisect
    from . import bas as bas_module
    from .models import Account

    path = _resolve_ledger(ctx.obj)
    sie = store_module.open_ledger(path)

    if any(a.number == number for a in sie.accounts):
        click.echo(f'Account {number} already exists in the chart of accounts.', err=True)
        sys.exit(1)

    bas_entry = bas_module.lookup(number)
    bas_name  = bas_entry[0] if bas_entry else ''
    bas_ktyp  = bas_entry[1] if bas_entry else bas_module.ktyp_for(number)

    if bas_name:
        click.echo(f'\nBAS: {number}  {bas_name}  [{bas_ktyp}]')
    else:
        click.echo(f'\nAccount {number} is not in the BAS table — enter a name manually.')

    name = click.prompt('Name', default=bas_name, show_default=bool(bas_name)).strip()
    if not name:
        click.echo('Name is required — aborted.')
        return

    click.echo(f'\n  {number:<6}  {bas_ktyp:^4}  {name}')
    if not click.confirm('Add account?', default=True):
        click.echo('Aborted.')
        return

    new_acc = Account(number=number, label=name, ktyp=bas_ktyp)
    numbers = [a.number for a in sie.accounts]
    idx = bisect.bisect_left(numbers, number)
    sie.accounts.insert(idx, new_acc)

    store_module.save_ledger(path, sie)
    click.echo(f'Added {number} "{name}" to {os.path.basename(store_module.db_path(path))}')


@accounts_group.command('rename')
@click.argument('number')
@click.pass_context
def accounts_rename(ctx, number):
    """Rename an existing account.

    The current name and the BAS standard name (if available) are shown as
    context before you enter the new name.
    """
    from . import bas as bas_module

    path = _resolve_ledger(ctx.obj)
    sie = store_module.open_ledger(path)

    acc = next((a for a in sie.accounts if a.number == number), None)
    if acc is None:
        click.echo(f'Account {number} not found in the chart of accounts.', err=True)
        sys.exit(1)

    bas_entry = bas_module.lookup(number)
    bas_name  = bas_entry[0] if bas_entry else None

    click.echo(f'\nAccount {number}')
    click.echo(f'  Current name : {acc.label}')
    if bas_name and bas_name != acc.label:
        click.echo(f'  BAS standard : {bas_name}')

    default = bas_name if bas_name else acc.label
    new_name = click.prompt('New name', default=default).strip()
    if not new_name:
        click.echo('Name is required — aborted.')
        return
    if new_name == acc.label:
        click.echo('Name unchanged — nothing to do.')
        return

    click.echo(f'\n  {number}  {acc.label!r}  →  {new_name!r}')
    if not click.confirm('Save?', default=True):
        click.echo('Aborted.')
        return

    acc.label = new_name
    store_module.save_ledger(path, sie)
    click.echo(f'Renamed {number} in {os.path.basename(store_module.db_path(path))}')


# ─── underlag ────────────────────────────────────────────────────────────────

def _parse_ref(ref: str) -> tuple[str, int]:
    """Parse 'A:5' or '5' into (series, number)."""
    if ':' in ref:
        series, num_str = ref.split(':', 1)
    else:
        series, num_str = 'A', ref
    if not num_str.isdigit():
        click.echo(f'Invalid voucher reference: {ref}  (expected e.g. A:5 or 5)', err=True)
        sys.exit(1)
    return series, int(num_str)


@cli.group()
@click.pass_context
def underlag(ctx):
    """Manage supporting documents (underlag) for vouchers."""
    pass


@underlag.command('add')
@click.argument('ref')
@click.argument('files', nargs=-1, required=True,
                type=click.Path(exists=True, dir_okay=False))
@click.pass_context
def underlag_add(ctx, ref, files):
    """Attach one or more files to a voucher.

    REF: voucher reference, e.g. A:5 or 5

    Example: bokforing underlag add A:5 receipt.pdf scan2.pdf
    """
    path = _resolve_ledger(ctx.obj)
    series, number = _parse_ref(ref)

    for src in files:
        stored = underlag_module.add_file(path, series, number, src)
        click.echo(f'Stored: {stored}  ←  {os.path.basename(src)}')


@underlag.command('list')
@click.argument('ref', required=False, default=None)
@click.pass_context
def underlag_list(ctx, ref):
    """List stored underlag.

    Without REF: summary of all vouchers that have underlag.
    With REF (e.g. A:5): list files for that specific voucher.
    """
    path = _resolve_ledger(ctx.obj)

    if ref:
        series, number = _parse_ref(ref)
        files = underlag_module.list_for_voucher(path, series, number)
        if not files:
            click.echo(f'No underlag for {series}:{number}.')
            return
        click.echo(f'\nUnderlag for {series}:{number}')
        click.echo(f'  {"ID":>4}  {"Filename":<40}  {"Original":<30}  Added')
        click.echo('  ' + '─' * 82)
        for f in files:
            click.echo(f'  {f["id"]:>4}  {f["filename"]:<40}  '
                       f'{f["original_name"]:<30}  {f["added_at"]}')
    else:
        rows = underlag_module.list_all(path)
        if not rows:
            click.echo('No underlag stored yet.')
            return
        click.echo(f'\nUnderlag summary — {os.path.basename(path)}')
        click.echo(f'  {"Voucher":<8}  {"Files":>5}')
        click.echo('  ' + '─' * 16)
        for r in rows:
            click.echo(f'  {r["series"]}:{r["number"]:<6}  {r["count"]:>5}')
    click.echo()


@underlag.command('open')
@click.argument('ref')
@click.pass_context
def underlag_open(ctx, ref):
    """Open all underlag files for a voucher with the system viewer."""
    path = _resolve_ledger(ctx.obj)
    series, number = _parse_ref(ref)
    files = underlag_module.list_for_voucher(path, series, number)

    if not files:
        click.echo(f'No underlag for {series}:{number}.')
        return

    for f in files:
        tmp_path = underlag_module.open_file(path, f['id'])
        if tmp_path:
            click.echo(f'Opening {f["original_name"]} …')
            subprocess.Popen(['xdg-open', tmp_path])
        else:
            click.echo(f'Could not read file id {f["id"]}.', err=True)


@underlag.command('remove')
@click.argument('file_id', type=int)
@click.pass_context
def underlag_remove(ctx, file_id):
    """Remove a stored underlag file by its ID (see 'underlag list')."""
    path = _resolve_ledger(ctx.obj)
    deleted = underlag_module.remove_file(path, file_id)
    if deleted:
        click.echo(f'Removed: {deleted}')
    else:
        click.echo(f'No file with ID {file_id}.', err=True)


# ─── bokslut ──────────────────────────────────────────────────────────────────

@cli.group('bokslut')
@click.pass_context
def bokslut(ctx):
    """Year-end closing commands."""
    pass


@bokslut.command('skatt')
@click.option('--skattesats', default='0.206', show_default=True, metavar='SATS',
              help='Bolagsskattesats, t.ex. 0.206')
@click.option('--statslanerantan', required=True, metavar='RÄNTA',
              help='Statslåneräntan 30 nov föregående år, t.ex. 0.0262')
@click.option('--konto-ar', 'konto_ar', multiple=True, metavar='KONTO:ÅR',
              help='Mappa periodiseringsfondkonto till inkomstår (t.ex. 2115:2015). Kan upprepas.')
@click.option('--series', default='A', show_default=True,
              help='Verifikationsserie att använda.')
@click.pass_context
def bokslut_skatt(ctx, skattesats, statslanerantan, konto_ar, series):
    """Beräkna bolagsskatt, visa beräkningsflödet och skapa verifikation.

    Erbjuder att boka Debet 8910 / Kredit 2512 och bifogar beräkningsflödet
    som underlag (.txt) till verifikationen.
    """
    import tempfile
    from .skatt import berakna_skatt

    path = _resolve_ledger(ctx.obj)
    sie = store_module.open_ledger(path)

    try:
        sats = Decimal(skattesats)
    except InvalidOperation:
        raise click.BadParameter(f'Ogiltigt tal: {skattesats!r}', param_hint='--skattesats')
    try:
        rantan = Decimal(statslanerantan)
    except InvalidOperation:
        raise click.BadParameter(f'Ogiltigt tal: {statslanerantan!r}', param_hint='--statslanerantan')

    year_map: dict[str, int] = {}
    for entry in konto_ar:
        parts = entry.split(':')
        if len(parts) != 2:
            raise click.BadParameter(
                f'Förväntar KONTO:ÅR, fick {entry!r}', param_hint='--konto-ar')
        konto, ar = parts[0].strip(), parts[1].strip()
        if not ar.isdigit() or len(ar) != 4:
            raise click.BadParameter(
                f'Ogiltigt år {ar!r} (förväntar fyrsiffrigt år)', param_hint='--konto-ar')
        year_map[konto] = int(ar)

    b = berakna_skatt(sie, sats, rantan, year_map)

    acct_map = sie.account_map()

    def _label(nr: str) -> str:
        a = acct_map.get(nr)
        return f' {a.label}' if a else ''

    def _amt(v: Decimal) -> str:
        return f'{v:>16,.2f}'

    captured: list[str] = []

    def _out(text: str = '') -> None:
        captured.append(text)
        click.echo(text)

    def _row(label: str, amount: Decimal, note: str = '') -> None:
        note_str = f'   {note}' if note else ''
        _out(f'  {label:<44}{_amt(amount)}{note_str}')

    W = 64
    sep = '─' * W
    dbl = '═' * W

    header = f'Skatteberäkning — {sie.company_name}' if sie.company_name else 'Skatteberäkning'
    _out()
    _out(header)
    if sie.year_begins and sie.year_ends:
        yb = _fmt_date(sie.year_begins)
        ye = _fmt_date(sie.year_ends)
        _out(f'Räkenskapsår: {yb} – {ye}')
    _out(dbl)
    _out()

    _row('Resultat före skatt (konto 3000–8899)', b.res_fore_skatt)
    _out()
    _out('  Skattemässiga justeringar:')

    _Z = Decimal('0')

    if b.raw_8314 != _Z:
        _row('    Intäktsränta skattekontot (8314)', b.raw_8314, 'skattefri')

    if b.raw_8423 != _Z:
        _row('    Räntekostnader skatter (8423)', b.raw_8423, 'ej avdragsgill')

    note_sch = f'({_amt(b.pf_ib_total).strip()} × {float(b.statslanerantan)*100:.2f}%)'
    _row('    Schablonintäkt periodiseringsfond', b.schablonintakt, note_sch)

    if b.upprakning_posts:
        _out('    Uppräkning vid återföring:')
        for p in b.upprakning_posts:
            pct = f'{float(p.factor - 1)*100:.0f}%'
            note_up = f'({_amt(p.aterfort).strip()} återfört × {pct})'
            _row(f'      {p.account} år {p.income_year} faktor {p.factor}',
                 p.upprakning, note_up)

    _out()
    _out(f'  {sep}')
    _row('Skattemässigt resultat', b.skattbart_resultat)
    _row('    Avrundat nedåt, närmaste 10 kr', b.skattbart_avrundat)
    _out(f'  × {float(b.skattesats)*100:.1f}%')
    _out(f'  {sep}')
    _row('Beräknad bolagsskatt', b.bolagsskatt_beraknad)
    _row('    Avrundat nedåt till närmaste krona', b.bolagsskatt)

    if b.bolagsskatt != _Z:
        _out()
        _out(f'  Föreslagen verifikation:')
        _out(f'  Debet  8910{_label("8910")}   {_amt(b.bolagsskatt).strip()}')
        _out(f'  Kredit 2512{_label("2512")}   {_amt(-b.bolagsskatt).strip()}')
        _out()

        if click.confirm('Skapa verifikation?', default=True):
            year = sie.year_ends[:4] if sie.year_ends else _today()[:4]
            default_date = sie.year_ends if sie.year_ends else _today()
            vdate = click.prompt('Datum (YYYYMMDD)', default=default_date)
            vlabel = click.prompt('Beskrivning', default=f'Bolagsskatt {year}')

            from .models import Voucher, Transaction
            num = next_voucher_number(sie, series)
            voucher = Voucher(
                series=series,
                number=num,
                date=vdate,
                label=vlabel,
                reg_date=_today(),
                signature='',
                transactions=[
                    Transaction(account='8910', amount=b.bolagsskatt,
                                date=vdate, label=vlabel),
                    Transaction(account='2512', amount=-b.bolagsskatt,
                                date=vdate, label=vlabel),
                ],
            )
            sie.vouchers.append(voucher)
            store_module.save_ledger(path, sie)
            click.echo(f'Sparad som {series}:{num}')

            tmpdir = tempfile.mkdtemp()
            tmp_path = os.path.join(tmpdir, f'skatteberakning_{year}.txt')
            try:
                with open(tmp_path, 'w', encoding='utf-8') as f:
                    f.write('\n'.join(captured))
                stored = underlag_module.add_file(path, series, num, tmp_path)
                click.echo(f'Underlag sparat: {stored}')
            finally:
                os.unlink(tmp_path)
                os.rmdir(tmpdir)

    elif b.skattbart_resultat <= _Z:
        click.echo('  (Skattemässigt underskott – ingen bolagsskatt detta år)')

    click.echo()


# ─── moms ────────────────────────────────────────────────────────────────────

@cli.command('moms')
@click.option('--from', 'from_date', required=True, metavar='YYYYMMDD',
              help='Periodens startdatum (inklusivt).')
@click.option('--to',   'to_date',   required=True, metavar='YYYYMMDD',
              help='Periodens slutdatum (inklusivt).')
@click.option('--series', default='A', show_default=True,
              help='Verifikationsserie att använda.')
@click.pass_context
def moms_cmd(ctx, from_date, to_date, series):
    """Beräkna momsdeklaration, visa rutorna och skapa redovisningsverifikation.

    Summerar kontona 2610/2620/2630 (utgående moms) och 2640 (ingående moms)
    för perioden. Varje ruta avrundas separat till närmaste hela krona
    (ROUND_HALF_UP). Öresresten bokas på 3740.

    Erbjuder att skapa en redovisningsverifikation som nollställer
    momskontona mot 2650 (exakt deklarerat belopp) och 3740 (öresrest),
    samt bifogar utskriften som underlag.
    """
    import tempfile
    from .moms import berakna_moms

    _Z = Decimal('0')
    path = _resolve_ledger(ctx.obj)
    sie = store_module.open_ledger(path)

    m = berakna_moms(sie, from_date, to_date)

    acct_map = sie.account_map()

    def _label(nr: str) -> str:
        a = acct_map.get(nr)
        return f'  {a.label}' if a else ''

    def _amt(v: Decimal) -> str:
        return f'{v:>14,.2f}'

    captured: list[str] = []

    def _out(text: str = '') -> None:
        captured.append(text)
        click.echo(text)

    W = 64
    dbl = '═' * W
    sep = '─' * W

    header = f'Momsdeklaration — {sie.company_name}' if sie.company_name else 'Momsdeklaration'
    _out()
    _out(header)
    _out(f'Period: {_fmt_date(from_date)} – {_fmt_date(to_date)}')
    _out(dbl)
    _out()

    def _ruta(ruta: str, label: str, dec: Decimal, exact: Decimal) -> None:
        exact_note = ''
        if exact != _Z and exact != dec:
            exact_note = f'   (exakt {exact:,.2f})'
        _out(f'  Ruta {ruta:<4}  {label:<38}{_amt(dec)}{exact_note}')

    for rate, ruta_base, ruta_moms, dec_base, base_exact, dec_moms, raw_moms, nr in [
        ('25 %', '05', '10', m.dec_base_25, m.base_utg_25, m.dec_utg_25, m.raw_2610, '2610'),
        ('12 %', '06', '11', m.dec_base_12, m.base_utg_12, m.dec_utg_12, m.raw_2620, '2620'),
        (' 6 %', '07', '12', m.dec_base_6,  m.base_utg_6,  m.dec_utg_6,  m.raw_2630, '2630'),
    ]:
        _ruta(ruta_base, f'Momspliktig försäljning {rate}', dec_base, base_exact)
        _ruta(ruta_moms, f'{nr}{_label(nr)}', dec_moms, abs(raw_moms))
        _out()

    _ruta('08', 'Momsfri försäljning', m.dec_base_momsfri, m.base_momsfri)
    _out()
    _out('  Ingående moms:')
    _ruta('30', f'2640{_label("2640")}', m.dec_ing, abs(m.raw_2640))
    _out()
    _out(f'  {sep}')
    netto_note = '   (att betala)' if m.dec_netto > _Z else ('   (återbetalning)' if m.dec_netto < _Z else '')
    _out(f'  Ruta 49    {"Moms att betala / återfå":<38}{_amt(m.dec_netto)}{netto_note}')
    _out()

    no_activity = (m.dec_netto == _Z and m.amount_3740 == _Z
                   and m.dec_base_momsfri == _Z)
    if no_activity:
        _out('  (Inga momstransaktioner i perioden.)')
        _out()
        return

    def _vline(debet_kredit: str, nr: str, amount: Decimal, note: str = '') -> None:
        note_str = f'   {note}' if note else ''
        _out(f'  {debet_kredit:<6} {nr}{_label(nr):<36} {_amt(abs(amount))}{note_str}')

    _out('  Föreslagen redovisningsverifikation:')
    for nr, raw in [('2610', m.raw_2610), ('2620', m.raw_2620), ('2630', m.raw_2630)]:
        if raw != _Z:
            clearing = -raw
            dk = 'Debet' if clearing > _Z else 'Kredit'
            _vline(dk, nr, clearing)
    if m.raw_2640 != _Z:
        clearing_2640 = -m.raw_2640
        dk = 'Debet' if clearing_2640 > _Z else 'Kredit'
        _vline(dk, '2640', clearing_2640)
    dk_2650 = 'Debet' if m.amount_2650 > _Z else 'Kredit'
    _vline(dk_2650, '2650', m.amount_2650, 'avrundat deklarationsbelopp')
    if m.amount_3740 != _Z:
        dk_3740 = 'Debet' if m.amount_3740 > _Z else 'Kredit'
        _vline(dk_3740, '3740', m.amount_3740, 'öresavrundning')
    _out()

    if click.confirm('Skapa verifikation?', default=True):
        default_date = to_date
        vdate = click.prompt('Datum (YYYYMMDD)', default=default_date)
        period_str = f'{_fmt_date(from_date)}–{_fmt_date(to_date)}'
        vlabel = click.prompt('Beskrivning', default=f'Momsredovisning {period_str}')

        from .models import Voucher, Transaction
        num = next_voucher_number(sie, series)
        trans: list[Transaction] = []
        for nr, raw in [('2610', m.raw_2610), ('2620', m.raw_2620), ('2630', m.raw_2630)]:
            if raw != _Z:
                trans.append(Transaction(account=nr, amount=-raw, date=vdate, label=vlabel))
        if m.raw_2640 != _Z:
            trans.append(Transaction(account='2640', amount=-m.raw_2640, date=vdate, label=vlabel))
        trans.append(Transaction(account='2650', amount=m.amount_2650, date=vdate, label=vlabel))
        if m.amount_3740 != _Z:
            trans.append(Transaction(account='3740', amount=m.amount_3740, date=vdate, label=vlabel))

        voucher = Voucher(
            series=series,
            number=num,
            date=vdate,
            label=vlabel,
            reg_date=_today(),
            signature='',
            transactions=trans,
        )
        sie.vouchers.append(voucher)
        store_module.save_ledger(path, sie)
        click.echo(f'Sparad som {series}:{num}')

        tmpdir = tempfile.mkdtemp()
        tmp_path = os.path.join(tmpdir, f'momsdeklaration_{from_date}_{to_date}.txt')
        try:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(captured))
            stored = underlag_module.add_file(path, series, num, tmp_path)
            click.echo(f'Underlag sparat: {stored}')
        finally:
            os.unlink(tmp_path)
            os.rmdir(tmpdir)

    click.echo()
