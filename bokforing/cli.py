"""CLI commands for the bokforing accounting app."""
from __future__ import annotations
import glob
import os
import subprocess
import sys
from datetime import date
from decimal import Decimal, InvalidOperation

import click

from . import underlag as underlag_module
from .reports import generate_balansrapport, generate_resultatrapport

from . import sie as sie_module
from .ledger import (find_account, get_account_history, get_balances,
                     init_from_previous, next_voucher_number, sort_vouchers)
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
    files = glob.glob('*.se')
    if len(files) == 1:
        return files[0]
    if len(files) > 1:
        click.echo('Multiple .se files found. Specify one with --ledger:', err=True)
        for f in sorted(files):
            click.echo(f'  {f}', err=True)
    else:
        click.echo('No .se ledger file found in current directory.', err=True)
        click.echo('Use: bokforing init --from-sie <previous.se> <year>', err=True)
    sys.exit(1)


@click.group()
@click.option('--ledger', '-l', default=None, metavar='FILE',
              help='SIE ledger file (auto-detected if not set)')
@click.pass_context
def cli(ctx, ledger):
    """Bokforing — CLI accounting backed by SIE 4 files."""
    ctx.ensure_object(dict)
    ctx.obj['ledger'] = ledger


# ─── init ────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument('year', type=int)
@click.option('--from-sie', '-f', required=True, metavar='FILE',
              help='Previous year SIE file to carry balances from')
@click.option('--output', '-o', default=None, metavar='FILE',
              help='Output filename (default: ledger_YYYY.se)')
def init(year, from_sie, output):
    """Create a new ledger year from a previous year's closing balances.

    Example: bokforing init 2024 --from-sie ../retsinaconsultingab_2023.se
    """
    if not os.path.exists(from_sie):
        click.echo(f'Error: {from_sie} not found', err=True)
        sys.exit(1)

    prev = sie_module.parse(from_sie)
    new_sie, source = init_from_previous(prev, f'{year}0101', f'{year}1231')

    if output is None:
        output = f'ledger_{year}.se'

    if os.path.exists(output):
        if not click.confirm(f'{output} already exists. Overwrite?', default=False):
            click.echo('Aborted.')
            return

    sie_module.write(output, new_sie)

    # Summarise the opening balance sheet
    assets = sum(v for k, v in new_sie.ib.items()
                 if k.isdigit() and int(k) < 2000)
    equity = sum(v for k, v in new_sie.ib.items()
                 if k.isdigit() and 2000 <= int(k) < 3000)
    diff = assets + equity

    click.echo(f'Created {output}')
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
    sie = sie_module.parse(path)
    account_map = sie.account_map()

    click.echo(f'\nAdding voucher to {path}')

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
    sie_module.append_voucher(path, voucher)
    click.echo(f'Saved as A:{num} in {path}')


# ─── balance ─────────────────────────────────────────────────────────────────

@cli.command()
@click.argument('filter', required=False, default=None, metavar='[PREFIX]')
@click.pass_context
def balance(ctx, filter):
    """Show current account balances.

    Optionally filter by account number prefix, e.g. 'balance 1' for assets.
    """
    path = _resolve_ledger(ctx.obj)
    sie = sie_module.parse(path)
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
    sie = sie_module.parse(path)

    vouchers = sie.vouchers if show_all else sie.vouchers[-n:]
    total = len(sie.vouchers)

    click.echo(f'\nVouchers — {path}  ({total} total)')
    click.echo(f'  {"Ref":<7}  {"Date":10}  {"Description":<36}  {"Debit":>12}')
    click.echo('  ' + '─' * 72)
    for v in vouchers:
        debit = sum(t.amount for t in v.transactions if t.amount > 0)
        click.echo(f'  {v.series}:{v.number:<5}  {_fmt_date(v.date):10}  {v.label:<36}  {debit:>12,.2f}')
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
    sie = sie_module.parse(path)
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


# ─── history ─────────────────────────────────────────────────────────────────

@cli.command()
@click.argument('account')
@click.pass_context
def history(ctx, account):
    """Show transaction history and running balance for an account."""
    path = _resolve_ledger(ctx.obj)
    sie = sie_module.parse(path)

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
    sie = sie_module.parse(path)

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
        lbl = click.prompt('  Label', default=t.get('label', ''), show_default=False)
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
    sie  = sie_module.parse(path)
    account_map = sie.account_map()

    click.echo(f'Analysing {os.path.basename(file)} …')
    try:
        suggestion = _suggest(file, sie)
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
    sie_module.append_voucher(path, voucher)
    click.echo(f'Saved as {series}:{num} in {os.path.basename(path)}')

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
    sie  = sie_module.parse(path)
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

    # ── AI batch call ─────────────────────────────────────────────────────
    click.echo('Analysing with Claude…')
    try:
        suggestions = _suggest_batch(transactions, sie, opening_balance)
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
        sie_module.append_voucher(path, voucher)
        # Re-parse so next_voucher_number is correct for the next iteration
        sie = sie_module.parse(path)
        saved += 1
        click.echo(f'Saved as {series}:{num}')

    click.echo(f'\nDone — {saved} voucher(s) saved, {skipped} skipped.')


# ─── sort ────────────────────────────────────────────────────────────────────

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
    sie = sie_module.parse(path)

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

    if dry_run:
        click.echo('\nDry run — no changes written.')
        return

    click.echo()
    if not click.confirm(f'Rewrite {os.path.basename(path)} and rename underlag?',
                         default=True):
        click.echo('Aborted.')
        return

    sie_module.write(path, new_sie)

    n_files = underlag_module.renumber_vouchers(path, renumber_map)

    click.echo(f'Done.')
    click.echo(f'  {len(renumber_map)} vouchers renumbered')
    if n_files:
        click.echo(f'  {n_files} underlag file(s) renamed')


# ─── report ──────────────────────────────────────────────────────────────────

@cli.command()
@click.option('--prev-sie', '-p', default=None, metavar='FILE',
              help='Previous year SIE file for comparison column')
@click.option('--output', '-o', default=None, metavar='FILE',
              help='Output .ods file (default: Resultatrapport_YYYY-MM-DD-YYYY-MM-DD.ods)')
@click.pass_context
def report(ctx, prev_sie, output):
    """Generate a Resultatrapport (income statement) as a LibreOffice ODS file."""
    path = _resolve_ledger(ctx.obj)
    sie = sie_module.parse(path)

    prev = None
    if prev_sie:
        if not os.path.exists(prev_sie):
            click.echo(f'Error: {prev_sie} not found', err=True)
            sys.exit(1)
        prev = sie_module.parse(prev_sie)

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
    sie = sie_module.parse(path)

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
    sie  = sie_module.parse(path)

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
              help='Output .se file (default: derived from company name and year)')
@click.pass_context
def sie5import(ctx, si5_file, output):
    """Restore a ledger year from a SIE 5 package (.si5).

    Writes a SIE 4 .se file and repopulates the underlag store with any
    documents embedded in the package.

    Note: #SRU codes are not stored in SIE 5 and will not be present
    in the restored SIE 4 file.
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

    if os.path.exists(output):
        if not click.confirm(f'{output} already exists. Overwrite?', default=False):
            click.echo('Aborted.')
            return

    sie, n_docs = restore_from_sie5(si5_file, output)

    click.echo(f'Restored {output}')
    click.echo(f'  Company : {sie.company_name}  ({sie.org_nr})')
    click.echo(f'  Period  : {_fmt_date(sie.year_begins)} – {_fmt_date(sie.year_ends)}')
    click.echo(f'  Accounts: {len(sie.accounts)}  |  Vouchers: {len(sie.vouchers)}')
    click.echo(f'  IB entries: {len(sie.ib)}  |  UB entries: {len(sie.ub)}')
    click.echo(f'  Underlag documents restored: {n_docs}')
    if not sie.accounts[0].sru if sie.accounts else True:
        click.echo('  Note: SRU codes are not stored in SIE 5 — not present in restored file')


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
    _, db_path = underlag_module._paths(path)

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

    underlag_dir, _ = underlag_module._paths(path)
    for f in files:
        filepath = os.path.join(underlag_dir, f['filename'])
        click.echo(f'Opening {f["filename"]} …')
        subprocess.Popen(['xdg-open', filepath])


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
