"""CLI commands for the bokforing accounting app."""
from __future__ import annotations
import glob
import os
import sys
from datetime import date
from decimal import Decimal, InvalidOperation

import click

from . import sie as sie_module
from .ledger import (find_account, get_account_history, get_balances,
                     init_from_previous, next_voucher_number)
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
    new_sie = init_from_previous(prev, f'{year}0101', f'{year}1231')

    if output is None:
        output = f'ledger_{year}.se'

    if os.path.exists(output):
        if not click.confirm(f'{output} already exists. Overwrite?', default=False):
            click.echo('Aborted.')
            return

    sie_module.write(output, new_sie)
    click.echo(f'Created {output}')
    click.echo(f'  Period : {year}-01-01 – {year}-12-31')
    click.echo(f'  Company: {new_sie.company_name}  ({new_sie.org_nr})')
    click.echo(f'  Opening balances carried from: {from_sie}')
    click.echo(f'  {len(new_sie.ib)} IB entries, {len(new_sie.accounts)} accounts')


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
    total_liab = Decimal('0')

    for acct in accounts:
        bal = balances[acct]
        name = _acc_name(account_map, acct)
        click.echo(f'  {acct:<6}  {name:<36}  {_fmt_amount(bal)}')
        if acct.startswith('1'):
            total_assets += bal
        elif acct.startswith('2'):
            total_liab += bal

    click.echo('─' * 62)
    if not filter:
        click.echo(f'  {"Assets (1xxx)":<44}  {_fmt_amount(total_assets)}')
        click.echo(f'  {"Liabilities/equity (2xxx)":<44}  {_fmt_amount(total_liab)}')
        diff = total_assets + total_liab
        color = 'green' if diff == 0 else 'red'
        label_diff = 'Balanced ✓' if diff == 0 else f'Difference (!)'
        click.echo(click.style(f'  {label_diff:<44}  {_fmt_amount(diff)}', fg=color))
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
