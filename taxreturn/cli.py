"""taxreturn CLI commands."""
from __future__ import annotations

import glob
import json
import os
import re
import sys
from decimal import Decimal
from pathlib import Path

import click
import yaml

from bokforing import store as store_module

_DATA = Path(__file__).parent / 'data'
_Z = Decimal('0')


def _resolve_ledger(ctx_obj: dict) -> str:
    path = ctx_obj.get('ledger')
    if path:
        return path
    files = glob.glob('*_ledger.db') or glob.glob('*.se')
    if len(files) == 1:
        return files[0]
    if len(files) > 1:
        raise click.ClickException(
            'Multiple ledger files found. Specify one with --ledger.'
        )
    raise click.ClickException('No ledger file found. Use --ledger.')


def _supplement_path(ledger_path: str) -> str:
    """Derive supplement YAML path from ledger path."""
    p = Path(ledger_path)
    stem = re.sub(r'_ledger$', '', p.stem)
    return str(p.parent / f'{stem}_taxreturn.yaml')


def _load_supplement(path: str) -> dict:
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    return {}


def _require_data() -> None:
    for name in ('ink2_fields.json', 'ink2r_fields.json', 'ink2s_fields.json', 'bas_ink2r.json'):
        if not (_DATA / name).exists():
            raise click.ClickException(
                f'Data file {name} missing. Run: taxreturn update-data'
            )


# ─── group ────────────────────────────────────────────────────────────────────

@click.group('taxreturn')
@click.pass_context
def taxreturn(ctx: click.Context) -> None:
    """INK2 tax return — generate SRU files for Skatteverket."""
    ctx.ensure_object(dict)


# ─── init ─────────────────────────────────────────────────────────────────────

@taxreturn.command('init')
@click.pass_context
def taxreturn_init(ctx: click.Context) -> None:
    """Create a taxreturn supplement YAML with default parameters."""
    ledger = ctx.obj.get('ledger') or _resolve_ledger(ctx.obj)
    sup_path = _supplement_path(ledger)
    if os.path.exists(sup_path):
        raise click.ClickException(f'{sup_path} already exists')
    content = """\
# Tax return supplement — edit before running taxreturn generate.
statslanerantan: "0.0250"   # Statslåneränta vid årets ingång (from Riksbanken)
skattesats: "0.206"         # Bolagsskattesats
konto_ar: {}                # Periodiseringsfond vintage overrides: {account: year}
uppdragstagare: true        # Uppdragstagare (e.g. redovisningskonsult) biträtt: yes/no
revision: false             # Årsredovisning föremål för revision: yes/no
program: "ClaudFinger"
version: "0.1"
# manual_fields: {}         # Override any SRU field: {field_code: value}
"""
    Path(sup_path).write_text(content, encoding='utf-8')
    click.echo(f'Created {sup_path}')


# ─── show ─────────────────────────────────────────────────────────────────────

@taxreturn.command('show')
@click.option('--supplement', '-s', default=None, metavar='FILE',
              help='Supplement YAML (default: ledger_YYYY_taxreturn.yaml)')
@click.pass_context
def taxreturn_show(ctx: click.Context, supplement: str | None) -> None:
    """Display all INK2 form fields and computed values."""
    _require_data()
    from .loader import load, _load_json

    ledger = ctx.obj.get('ledger') or _resolve_ledger(ctx.obj)
    sup_path = supplement or _supplement_path(ledger)
    supp = _load_supplement(sup_path)
    sie = store_module.open_ledger(ledger)
    tr = load(sie, supp)

    ink2_meta  = _load_json('ink2_fields.json')
    ink2r_meta = _load_json('ink2r_fields.json')
    ink2s_meta = _load_json('ink2s_fields.json')

    def _section(title: str, fields: dict, meta: dict) -> None:
        click.echo(f'\n  ── {title} ──')
        for code, val in sorted(fields.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 0):
            desc = meta.get(code, {}).get('attr', '')
            click.echo(f'  {code:>6}  {val!s:>14}  {desc}')

    click.echo(f'\nINK2 – {tr.period}  ({tr.org_nr}  {tr.company_name})')
    click.echo(f'  Räkenskapsår: {tr.year_begins[:4]}-{tr.year_begins[4:6]}-{tr.year_begins[6:]}')
    click.echo(f'              – {tr.year_ends[:4]}-{tr.year_ends[4:6]}-{tr.year_ends[6:]}')

    _section('INK2 (sida 1)',  tr.ink2_fields,  ink2_meta)
    _section('INK2R (sida 2–4 räkenskapsschema)', tr.ink2r_fields, ink2r_meta)
    _section('INK2S (sida 5 skattemässiga justeringar)', tr.ink2s_fields, ink2s_meta)

    if tr.warnings:
        click.echo('\n  Varningar:')
        for w in tr.warnings:
            click.echo(f'  ⚠  {w}')


# ─── generate ─────────────────────────────────────────────────────────────────

@taxreturn.command('generate')
@click.option('--supplement', '-s', default=None, metavar='FILE',
              help='Supplement YAML (default: ledger_YYYY_taxreturn.yaml)')
@click.option('-o', '--output', default=None, metavar='DIR',
              help='Output directory (default: ledger_YYYY_taxreturn/)')
@click.pass_context
def taxreturn_generate(
    ctx: click.Context, supplement: str | None, output: str | None
) -> None:
    """Write INFO.SRU and BLANKETTER.SRU for Skatteverket submission."""
    _require_data()
    from .loader import load
    from .sru import write_info_sru, write_blanketter_sru

    ledger = ctx.obj.get('ledger') or _resolve_ledger(ctx.obj)
    sup_path = supplement or _supplement_path(ledger)
    if not os.path.exists(sup_path):
        raise click.ClickException(
            f'Supplement not found: {sup_path}\nRun: taxreturn init'
        )
    supp = _load_supplement(sup_path)
    sie = store_module.open_ledger(ledger)
    tr = load(sie, supp)

    # Determine output directory
    if output is None:
        p = Path(ledger)
        stem = re.sub(r'_ledger$', '', p.stem)
        out_dir = p.parent / f'{stem}_taxreturn'
    else:
        out_dir = Path(output)
    out_dir.mkdir(parents=True, exist_ok=True)

    info_path      = out_dir / 'INFO.SRU'
    blanketter_path = out_dir / 'BLANKETTER.SRU'

    write_info_sru(sie, supp, info_path)
    write_blanketter_sru(tr, blanketter_path)

    click.echo(f'Wrote {info_path}')
    click.echo(f'Wrote {blanketter_path}')

    if tr.warnings:
        for w in tr.warnings:
            click.echo(f'Varning: {w}', err=True)


# ─── annotate ─────────────────────────────────────────────────────────────────

@taxreturn.command('annotate')
@click.option('--force', is_flag=True,
              help='Overwrite existing SRU codes')
@click.option('--yes', '-y', is_flag=True,
              help='Skip confirmation prompt')
@click.option('--dry-run', is_flag=True,
              help='Show what would change without writing')
@click.pass_context
def taxreturn_annotate(
    ctx: click.Context, force: bool, yes: bool, dry_run: bool
) -> None:
    """Write BAS→INK2R SRU codes into the ledger DB accounts.

    After running this, .se exports will include #SRU lines and the
    taxreturn generate command will use the stored codes directly.
    """
    _require_data()
    from .loader import _load_json, _find_ink2r_field

    ledger = ctx.obj.get('ledger') or _resolve_ledger(ctx.obj)
    sie = store_module.open_ledger(ledger)
    bas_map = _load_json('bas_ink2r.json')

    changes: list[tuple] = []      # (acct_number, old_sru, new_sru)
    skipped_existing: int = 0
    no_match: list[str] = []

    for acct in sie.accounts:
        fcode = None
        # Range match from bas_ink2r (ignore stored SRU for the lookup)
        n = int(acct.number)
        for fc, ranges in bas_map.items():
            for r in ranges:
                if r['min'] <= n <= r['max'] and n not in r.get('excl', []):
                    fcode = fc
                    break
            if fcode:
                break

        if fcode is None:
            if acct.number.isdigit() and 1000 <= int(acct.number) <= 8999:
                no_match.append(acct.number)
            continue

        if acct.sru and not force:
            skipped_existing += 1
            continue

        if acct.sru == [fcode]:
            continue  # already correct

        changes.append((acct.number, list(acct.sru), [fcode]))

    # Report
    if not changes:
        click.echo('No changes to make.')
        if skipped_existing:
            click.echo(f'  {skipped_existing} accounts already have SRU codes (use --force to overwrite)')
        return

    click.echo(f'{"DRY RUN — " if dry_run else ""}Changes to make: {len(changes)}')
    for acct_nr, old, new in sorted(changes):
        old_s = ', '.join(old) if old else '(none)'
        new_s = ', '.join(new)
        click.echo(f'  {acct_nr:>6}  {old_s:>8} → {new_s}')
    if skipped_existing:
        click.echo(f'  ({skipped_existing} accounts with existing codes skipped; use --force)')
    if no_match:
        click.echo(f'  ({len(no_match)} accounts have no INK2R mapping: {", ".join(sorted(no_match)[:10])}...)')

    if dry_run:
        return

    if not yes:
        click.confirm(f'\nWrite {len(changes)} SRU codes to {ledger}?', abort=True)

    # Apply changes
    acct_map = {a.number: a for a in sie.accounts}
    for acct_nr, _, new_sru in changes:
        acct_map[acct_nr].sru = new_sru

    store_module.save_ledger(ledger, sie)
    click.echo(f'Updated {len(changes)} accounts in {ledger}')


# ─── update-data ─────────────────────────────────────────────────────────────

@taxreturn.command('update-data')
@click.option('--skv-zip', default=None, metavar='FILE',
              help='Local Skatteverket ZIP (skips web download)')
@click.option('--bas-xlsx', default=None, metavar='FILE',
              help='Local bas.se INK2_P1 Excel (skips web download)')
def taxreturn_update_data(skv_zip: str | None, bas_xlsx: str | None) -> None:
    """Download latest XLS specs and regenerate taxreturn/data/*.json."""
    scripts_dir = Path(__file__).parent / 'scripts'
    sys.path.insert(0, str(scripts_dir.parent.parent))
    from taxreturn.scripts.fetch_data import run
    run(skv_zip, bas_xlsx)
