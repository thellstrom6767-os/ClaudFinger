"""Generate Resultatrapport and Balansrapport as LibreOffice Calc ODS files."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from odf.opendocument import OpenDocumentSpreadsheet
from odf.style import (Style, TextProperties, TableCellProperties,
                       TableColumnProperties, ParagraphProperties)
from odf.table import Table, TableRow, TableCell, TableColumn
from odf.text import P
from odf.namespaces import OFFICENS

from .models import SIEFile

Z = Decimal('0')


# ─── number formatting ────────────────────────────────────────────────────────

def _sek(v: Decimal) -> str:
    """Swedish SEK format: 1 234,56 or -1 234,56"""
    if v == Z:
        return '0,00'
    neg = v < Z
    whole, frac = f'{abs(v):.2f}'.split('.')
    groups: list[str] = []
    while len(whole) > 3:
        groups.append(whole[-3:])
        whole = whole[:-3]
    groups.append(whole)
    return ('-' if neg else '') + ' '.join(reversed(groups)) + ',' + frac


def _pct(v: Decimal, base: Decimal) -> str:
    if base == Z:
        return '0,0'
    return f'{float(v / base * 100):.1f}'.replace('.', ',')


def _jmf(curr: Decimal, prev: Decimal) -> str:
    if prev == Z:
        return '###.#' if curr != Z else '0,0'
    j = float(curr / prev * 100)
    return '###.#' if abs(j) >= 1000 else f'{j:.1f}'.replace('.', ',')


# ─── data extraction ──────────────────────────────────────────────────────────

def _results(sie: SIEFile) -> dict[str, Decimal]:
    """Sum transactions per account, negated to P&L display convention."""
    totals: dict[str, Decimal] = {}
    for v in sie.vouchers:
        for t in v.transactions:
            totals[t.account] = totals.get(t.account, Z) + t.amount
    return {k: -v for k, v in totals.items()}


def _labels(curr_sie: SIEFile, prev_sie: Optional[SIEFile]) -> dict[str, str]:
    m = {a.number: a.label for a in curr_sie.accounts}
    if prev_sie:
        for a in prev_sie.accounts:
            m.setdefault(a.number, a.label)
    return m


def _section_total(lo: int, hi: int, res: dict[str, Decimal]) -> Decimal:
    return sum(
        (v for k, v in res.items() if k.isdigit() and lo <= int(k) < hi),
        Z
    )


def _section_accounts(
    lo: int, hi: int,
    curr: dict[str, Decimal],
    prev: dict[str, Decimal],
    labels: dict[str, str],
) -> list[tuple[str, str, Decimal, Decimal]]:
    """Accounts in [lo, hi) with non-zero in either year, sorted by number."""
    keys = {k for k in list(curr) + list(prev) if k.isdigit() and lo <= int(k) < hi}
    rows = []
    for nr in sorted(keys):
        cv, pv = curr.get(nr, Z), prev.get(nr, Z)
        if cv != Z or pv != Z:
            rows.append((nr, labels.get(nr, nr), cv, pv))
    return rows


# ─── ODS style builder ────────────────────────────────────────────────────────

def _make_styles(doc: OpenDocumentSpreadsheet) -> dict[str, str]:
    """Register cell styles in the document, return name→name mapping."""
    def cell_style(name: str, bold=False, italic=False, halign='left',
                   fontsize=9, top_border=False, bg=None, color='#000000'):
        s = Style(name=name, family='table-cell')
        tp_attrs = {'fontsize': f'{fontsize}pt', 'color': color}
        if bold:
            tp_attrs['fontweight'] = 'bold'
        if italic:
            tp_attrs['fontstyle'] = 'italic'
        s.addElement(TextProperties(**tp_attrs))
        cp_attrs = {}
        if top_border:
            cp_attrs['bordertop'] = '0.5pt solid #000000'
        if bg:
            cp_attrs['backgroundcolor'] = bg
        if cp_attrs:
            s.addElement(TableCellProperties(**cp_attrs))
        s.addElement(ParagraphProperties(textalign=halign))
        doc.styles.addElement(s)
        return name

    styles = {}
    styles['title']      = cell_style('title',      bold=True, fontsize=13)
    styles['meta']       = cell_style('meta',        fontsize=8)
    styles['col_head']   = cell_style('col_head',    bold=True,  halign='right', fontsize=7)
    styles['col_date']   = cell_style('col_date',    italic=True, halign='right', fontsize=7, color='#444444')
    styles['sect']       = cell_style('sect',        bold=True, fontsize=9)
    styles['subsect']    = cell_style('subsect',     fontsize=9)
    styles['desc']       = cell_style('desc',        fontsize=8)       # indented account row
    styles['num']        = cell_style('num',         halign='right', fontsize=8)
    styles['pct']        = cell_style('pct',         italic=True, halign='right', fontsize=7, color='#555555')
    styles['sub_lbl']    = cell_style('sub_lbl',     bold=True, fontsize=8)
    styles['sub_num']    = cell_style('sub_num',     bold=True, halign='right', fontsize=8)
    styles['sub_pct']    = cell_style('sub_pct',     bold=False, italic=True, halign='right', fontsize=7, color='#555555')
    styles['total_lbl']  = cell_style('total_lbl',   bold=True, fontsize=9, top_border=True)
    styles['total_num']  = cell_style('total_num',   bold=True, halign='right', fontsize=9, top_border=True)
    styles['total_pct']  = cell_style('total_pct',   italic=True, halign='right', fontsize=7, top_border=True, color='#555555')
    styles['empty']      = cell_style('empty',       fontsize=8)
    styles['comp_lbl']   = cell_style('comp_lbl',    bold=True, italic=True, fontsize=8, color='#444444')
    styles['comp_num']   = cell_style('comp_num',    bold=True, italic=True, halign='right', fontsize=8, color='#444444')
    return styles


# ─── row helpers ─────────────────────────────────────────────────────────────

def _empty_row(sheet: Table, cols: int = 7):
    tr = TableRow()
    for _ in range(cols):
        tc = TableCell(stylename='empty')
        tc.addElement(P(text=''))
        tr.addElement(tc)
    sheet.addElement(tr)


def _text_cell(text: str, style: str) -> TableCell:
    tc = TableCell(stylename=style)
    tc.addElement(P(text=text))
    return tc


def _num_cell(val: str, style: str) -> TableCell:
    tc = TableCell(stylename=style)
    tc.addElement(P(text=val))
    return tc


def _account_row(sheet: Table, label: str,
                 curr: Decimal, prev: Decimal,
                 net_curr: Decimal, net_prev: Decimal):
    tr = TableRow()
    tr.addElement(_text_cell('  ' + label, 'desc'))
    tr.addElement(_num_cell(_sek(curr),           'num'))
    tr.addElement(_num_cell(_pct(curr, net_curr), 'pct'))
    tr.addElement(_num_cell(_sek(curr),           'num'))   # UTG SALDO = DENNA PERIOD (full year)
    tr.addElement(_num_cell(_pct(curr, net_curr), 'pct'))
    tr.addElement(_num_cell(_sek(prev),           'num'))
    tr.addElement(_num_cell(_jmf(curr, prev),     'pct'))
    sheet.addElement(tr)


def _subtotal_row(sheet: Table, title: str,
                  curr: Decimal, prev: Decimal,
                  net_curr: Decimal, net_prev: Decimal,
                  style_prefix: str = 'sub'):
    tr = TableRow()
    tr.addElement(_text_cell(title,                              f'{style_prefix}_lbl'))
    tr.addElement(_num_cell(_sek(curr),                          f'{style_prefix}_num'))
    tr.addElement(_num_cell(_pct(curr, net_curr),                f'{style_prefix}_pct'))
    tr.addElement(_num_cell(_sek(curr),                          f'{style_prefix}_num'))
    tr.addElement(_num_cell(_pct(curr, net_curr),                f'{style_prefix}_pct'))
    tr.addElement(_num_cell(_sek(prev),                          f'{style_prefix}_num'))
    tr.addElement(_num_cell(_jmf(curr, prev),                    f'{style_prefix}_pct'))
    sheet.addElement(tr)


# ─── main generator ───────────────────────────────────────────────────────────

def generate_resultatrapport(
    sie: SIEFile,
    prev_sie: Optional[SIEFile],
    output_path: str,
) -> None:
    curr = _results(sie)
    prev = _results(prev_sie) if prev_sie else {}
    labels = _labels(sie, prev_sie)

    doc = OpenDocumentSpreadsheet()
    styles = _make_styles(doc)

    sheet = Table(name='Resultatrapport')
    doc.spreadsheet.addElement(sheet)

    # Column widths
    for w in ['7.0cm', '2.6cm', '1.4cm', '2.6cm', '1.4cm', '2.6cm', '1.4cm']:
        col = TableColumn()
        col.setAttribute('stylename', 'co1')
        style = Style(name=f'co_{w}', family='table-column')
        style.addElement(TableColumnProperties(columnwidth=w))
        doc.automaticstyles.addElement(style)
        col.setAttribute('stylename', f'co_{w}')
        sheet.addElement(col)

    # ── Title / header ────────────────────────────────────────────────────
    tr = TableRow()
    tc = TableCell(stylename='title', numbercolumnsspanned='7')
    tc.addElement(P(text='Resultatrapport'))
    tr.addElement(tc)
    sheet.addElement(tr)

    period = (f'{sie.year_begins[:4]}-{sie.year_begins[4:6]}-{sie.year_begins[6:]}'
              f' – '
              f'{sie.year_ends[:4]}-{sie.year_ends[4:6]}-{sie.year_ends[6:]}')
    prev_end = (f'{prev_sie.year_ends[:4]}-{prev_sie.year_ends[4:6]}-{prev_sie.year_ends[6:]}'
                if prev_sie else '—')

    last_ver = sie.vouchers[-1] if sie.vouchers else None
    bokslut = (f'Bokslut {sie.year_ends[:6]} tom ver '
               f'{last_ver.series} {last_ver.number}') if last_ver else ''

    for text in [sie.company_name, bokslut, f'Räkenskapsår {period}']:
        tr = TableRow()
        tc = TableCell(stylename='meta', numbercolumnsspanned='7')
        tc.addElement(P(text=text))
        tr.addElement(tc)
        sheet.addElement(tr)

    _empty_row(sheet)

    # ── Column headers ────────────────────────────────────────────────────
    per_compact = f'{sie.year_begins[2:]}-{sie.year_ends[2:]}'
    tr = TableRow()
    tr.addElement(_text_cell('', 'empty'))
    tr.addElement(_text_cell('DENNA PERIOD', 'col_head'))
    tr.addElement(_text_cell('OMS%',         'col_head'))
    tr.addElement(_text_cell('UTG SALDO',    'col_head'))
    tr.addElement(_text_cell('OMS%',         'col_head'))
    tr.addElement(_text_cell('ACK FÖREG ÅR', 'col_head'))
    tr.addElement(_text_cell('JMF%',         'col_head'))
    sheet.addElement(tr)

    tr = TableRow()
    tr.addElement(_text_cell('', 'empty'))
    tr.addElement(_text_cell(per_compact,          'col_date'))
    tr.addElement(_text_cell('',                   'empty'))
    tr.addElement(_text_cell(f'=>{sie.year_ends[2:]}', 'col_date'))
    tr.addElement(_text_cell('',                   'empty'))
    tr.addElement(_text_cell(f'=>{prev_sie.year_ends[2:] if prev_sie else "—"}', 'col_date'))
    tr.addElement(_text_cell('',                   'empty'))
    sheet.addElement(tr)

    _empty_row(sheet)

    # ── helper closures ───────────────────────────────────────────────────
    net_curr = _section_total(3000, 4000, curr)
    net_prev = _section_total(3000, 4000, prev)

    def accounts(lo, hi):
        return _section_accounts(lo, hi, curr, prev, labels)

    def total(lo, hi, res=None):
        return _section_total(lo, hi, curr if res is None else res)

    def total_p(lo, hi):
        return _section_total(lo, hi, prev)

    # ── RÖRELSEINTÄKTER ───────────────────────────────────────────────────
    tr = TableRow()
    tr.addElement(_text_cell('RÖRELSEINTÄKTER', 'sect'))
    for _ in range(6):
        tr.addElement(_text_cell('', 'empty'))
    sheet.addElement(tr)

    # Försäljning 3000–3799
    försäljning_accts = accounts(3000, 3800)
    if försäljning_accts:
        tr = TableRow()
        tr.addElement(_text_cell('Försäljning', 'subsect'))
        for _ in range(6):
            tr.addElement(_text_cell('', 'empty'))
        sheet.addElement(tr)
        for nr, lbl, cv, pv in försäljning_accts:
            _account_row(sheet, f'{nr} {lbl}', cv, pv, net_curr, net_prev)

    sum_f_c, sum_f_p = total(3000, 3800), total_p(3000, 3800)
    _subtotal_row(sheet, 'Summa försäljning', sum_f_c, sum_f_p, net_curr, net_prev)

    # Övriga rörelseintäkter 3800–3999
    other_inc = accounts(3800, 4000)
    sum_oi_c, sum_oi_p = total(3800, 4000), total_p(3800, 4000)
    if other_inc or sum_oi_c != Z or sum_oi_p != Z:
        tr = TableRow()
        tr.addElement(_text_cell('Övriga rörelseintäkter', 'subsect'))
        for _ in range(6):
            tr.addElement(_text_cell('', 'empty'))
        sheet.addElement(tr)
        for nr, lbl, cv, pv in other_inc:
            _account_row(sheet, f'{nr} {lbl}', cv, pv, net_curr, net_prev)
        _subtotal_row(sheet, 'Summa övriga rörelseintäkter', sum_oi_c, sum_oi_p, net_curr, net_prev)

    _empty_row(sheet)
    summa_int_c = total(3000, 4000)
    summa_int_p = total_p(3000, 4000)
    _subtotal_row(sheet, 'SUMMA RÖRELSEINTÄKTER', summa_int_c, summa_int_p,
                  net_curr, net_prev, style_prefix='total')
    _empty_row(sheet)

    # ── RÖRELSEKOSTNADER ──────────────────────────────────────────────────
    tr = TableRow()
    tr.addElement(_text_cell('RÖRELSEKOSTNADER', 'sect'))
    for _ in range(6):
        tr.addElement(_text_cell('', 'empty'))
    sheet.addElement(tr)

    COST_SUBSECTIONS = [
        ('Material och varor',             4000, 5000, 'Summa material och varor'),
        ('Övriga externa rörelseutgifter', 5000, 7000, 'Summa övriga externa rörelseutgifter'),
        ('Personalkostnader',              7000, 7700, 'Summa personalkostnader'),
        ('Avskrivningar',                  7700, 8000, 'Summa avskrivningar'),
    ]

    running_cost_c = Z
    running_cost_p = Z
    bruttovinst_done = False

    for sub_title, lo, hi, sum_title in COST_SUBSECTIONS:
        accts = accounts(lo, hi)
        sc, sp = total(lo, hi), total_p(lo, hi)
        if not accts and sc == Z and sp == Z:
            continue
        tr = TableRow()
        tr.addElement(_text_cell(sub_title, 'subsect'))
        for _ in range(6):
            tr.addElement(_text_cell('', 'empty'))
        sheet.addElement(tr)
        for nr, lbl, cv, pv in accts:
            _account_row(sheet, f'{nr} {lbl}', cv, pv, net_curr, net_prev)
        _subtotal_row(sheet, sum_title, sc, sp, net_curr, net_prev)
        running_cost_c += sc
        running_cost_p += sp

        if sub_title == 'Material och varor' and not bruttovinst_done:
            _empty_row(sheet)
            _subtotal_row(sheet, 'Bruttovinst',
                          summa_int_c + sc, summa_int_p + sp,
                          net_curr, net_prev, style_prefix='total')
            _empty_row(sheet)
            bruttovinst_done = True

    _empty_row(sheet)
    _subtotal_row(sheet, 'SUMMA RÖRELSEKOSTNADER',
                  running_cost_c, running_cost_p, net_curr, net_prev, style_prefix='total')
    _empty_row(sheet)

    ror_res_c = summa_int_c + running_cost_c
    ror_res_p = summa_int_p + running_cost_p
    _subtotal_row(sheet, 'Rörelseresultat', ror_res_c, ror_res_p,
                  net_curr, net_prev, style_prefix='total')
    _empty_row(sheet)

    # ── Finansiella poster 8300–8499 ──────────────────────────────────────
    fin_accts = accounts(8300, 8500)
    fin_c, fin_p = total(8300, 8500), total_p(8300, 8500)
    if fin_accts or fin_c != Z or fin_p != Z:
        tr = TableRow()
        tr.addElement(_text_cell('Finansiella poster', 'subsect'))
        for _ in range(6):
            tr.addElement(_text_cell('', 'empty'))
        sheet.addElement(tr)
        for nr, lbl, cv, pv in fin_accts:
            _account_row(sheet, f'{nr} {lbl}', cv, pv, net_curr, net_prev)
        _subtotal_row(sheet, 'Summa finansiella poster', fin_c, fin_p, net_curr, net_prev)
        _empty_row(sheet)

    res_fin_c = ror_res_c + fin_c
    res_fin_p = ror_res_p + fin_p
    _subtotal_row(sheet, 'Resultat efter finansiella poster',
                  res_fin_c, res_fin_p, net_curr, net_prev, style_prefix='total')
    _empty_row(sheet)

    # ── Extraordinära poster 8700–8799 ────────────────────────────────────
    ext_accts = accounts(8700, 8800)
    ext_c, ext_p = total(8700, 8800), total_p(8700, 8800)
    if ext_accts or ext_c != Z or ext_p != Z:
        tr = TableRow()
        tr.addElement(_text_cell('Extraordinära poster', 'subsect'))
        for _ in range(6):
            tr.addElement(_text_cell('', 'empty'))
        sheet.addElement(tr)
        for nr, lbl, cv, pv in ext_accts:
            _account_row(sheet, f'{nr} {lbl}', cv, pv, net_curr, net_prev)
        _subtotal_row(sheet, 'Summa extraordinära poster', ext_c, ext_p, net_curr, net_prev)
        _empty_row(sheet)

    res_ext_c = res_fin_c + ext_c
    res_ext_p = res_fin_p + ext_p
    _subtotal_row(sheet, 'Resultat efter extraordinära poster',
                  res_ext_c, res_ext_p, net_curr, net_prev, style_prefix='total')
    _empty_row(sheet)

    # ── Bokslutsdispositioner 8800–8899 ───────────────────────────────────
    boks_accts = accounts(8800, 8900)
    boks_c, boks_p = total(8800, 8900), total_p(8800, 8900)
    if boks_accts or boks_c != Z or boks_p != Z:
        tr = TableRow()
        tr.addElement(_text_cell('Bokslutsdispositioner', 'subsect'))
        for _ in range(6):
            tr.addElement(_text_cell('', 'empty'))
        sheet.addElement(tr)
        for nr, lbl, cv, pv in boks_accts:
            _account_row(sheet, f'{nr} {lbl}', cv, pv, net_curr, net_prev)
        _subtotal_row(sheet, 'Summa bokslutsdispositioner', boks_c, boks_p, net_curr, net_prev)
        _empty_row(sheet)

    res_fore_skatt_c = res_ext_c + boks_c
    res_fore_skatt_p = res_ext_p + boks_p
    _subtotal_row(sheet, 'Resultat före skatt', res_fore_skatt_c, res_fore_skatt_p,
                  net_curr, net_prev, style_prefix='total')
    _empty_row(sheet)

    # ── Skatter 8900–8998 ─────────────────────────────────────────────────
    skatt_accts = accounts(8900, 8999)
    skatt_c, skatt_p = total(8900, 8999), total_p(8900, 8999)
    if skatt_accts or skatt_c != Z or skatt_p != Z:
        tr = TableRow()
        tr.addElement(_text_cell('Skatter', 'subsect'))
        for _ in range(6):
            tr.addElement(_text_cell('', 'empty'))
        sheet.addElement(tr)
        for nr, lbl, cv, pv in skatt_accts:
            _account_row(sheet, f'{nr} {lbl}', cv, pv, net_curr, net_prev)
        _subtotal_row(sheet, 'Summa skatter', skatt_c, skatt_p, net_curr, net_prev)
        _empty_row(sheet)

    # ── ÅRETS RESULTAT ────────────────────────────────────────────────────
    ar_res_c = res_fore_skatt_c + skatt_c
    ar_res_p = res_fore_skatt_p + skatt_p
    _subtotal_row(sheet, 'ÅRETS RESULTAT', ar_res_c, ar_res_p,
                  net_curr, net_prev, style_prefix='total')

    _empty_row(sheet)
    tr = TableRow()
    tc = TableCell(stylename='meta', numbercolumnsspanned='7')
    tc.addElement(P(text=f'Genererad {datetime.now().strftime("%Y-%m-%d %H:%M")}'))
    tr.addElement(tc)
    sheet.addElement(tr)

    doc.save(output_path)


# ═══════════════════════════════════════════════════════════════════════════════
# Balansrapport
# ═══════════════════════════════════════════════════════════════════════════════

def _bal_empty_row(sheet: Table):
    tr = TableRow()
    for _ in range(4):
        tc = TableCell(stylename='empty')
        tc.addElement(P(text=''))
        tr.addElement(tc)
    sheet.addElement(tr)


def _bal_account_row(sheet: Table, label: str,
                     ib_val: Decimal, per_val: Decimal, utg_val: Decimal,
                     indent: str = '  '):
    tr = TableRow()
    tr.addElement(_text_cell(indent + label, 'desc'))
    tr.addElement(_num_cell(_sek(ib_val),  'num'))
    tr.addElement(_num_cell(_sek(per_val), 'num'))
    tr.addElement(_num_cell(_sek(utg_val), 'num'))
    sheet.addElement(tr)


def _bal_subtotal_row(sheet: Table, title: str,
                      ib_val: Decimal, per_val: Decimal, utg_val: Decimal,
                      style_prefix: str = 'sub'):
    tr = TableRow()
    tr.addElement(_text_cell(title,         f'{style_prefix}_lbl'))
    tr.addElement(_num_cell(_sek(ib_val),   f'{style_prefix}_num'))
    tr.addElement(_num_cell(_sek(per_val),  f'{style_prefix}_num'))
    tr.addElement(_num_cell(_sek(utg_val),  f'{style_prefix}_num'))
    sheet.addElement(tr)


def generate_balansrapport(sie: SIEFile, output_path: str) -> None:
    """Generate a Balansrapport (balance sheet) as an ODS file."""

    # ── compute period changes from transactions ───────────────────────────
    period: dict[str, Decimal] = {}
    for v in sie.vouchers:
        for t in v.transactions:
            period[t.account] = period.get(t.account, Z) + t.amount

    labels = {a.number: a.label for a in sie.accounts}

    def ib(acc: str) -> Decimal:
        return sie.ib.get(acc, Z)

    def per(acc: str) -> Decimal:
        return period.get(acc, Z)

    def utg(acc: str) -> Decimal:
        return ib(acc) + per(acc)

    def section_rows(lo: int, hi: int) -> list[tuple[str, str, Decimal, Decimal, Decimal]]:
        keys = {k for k in list(sie.ib) + list(period)
                if k.isdigit() and lo <= int(k) < hi}
        rows = []
        for nr in sorted(keys):
            iv, pv, uv = ib(nr), per(nr), utg(nr)
            if iv != Z or pv != Z or uv != Z:
                rows.append((nr, labels.get(nr, nr), iv, pv, uv))
        return rows

    def section_totals(lo: int, hi: int) -> tuple[Decimal, Decimal, Decimal]:
        rows = section_rows(lo, hi)
        return (sum((r[2] for r in rows), Z),
                sum((r[3] for r in rows), Z),
                sum((r[4] for r in rows), Z))

    # ── build document ─────────────────────────────────────────────────────
    doc = OpenDocumentSpreadsheet()
    _make_styles(doc)

    sheet = Table(name='Balansrapport')
    doc.spreadsheet.addElement(sheet)

    for w in ['9.5cm', '2.8cm', '2.8cm', '2.8cm']:
        style = Style(name=f'bcol_{w}', family='table-column')
        style.addElement(TableColumnProperties(columnwidth=w))
        doc.automaticstyles.addElement(style)
        col = TableColumn()
        col.setAttribute('stylename', f'bcol_{w}')
        sheet.addElement(col)

    # ── header ────────────────────────────────────────────────────────────
    for text, style in [('Balansrapport', 'title'),
                        (sie.company_name, 'meta')]:
        tr = TableRow()
        tc = TableCell(stylename=style, numbercolumnsspanned='4')
        tc.addElement(P(text=text))
        tr.addElement(tc)
        sheet.addElement(tr)

    last_ver = sie.vouchers[-1] if sie.vouchers else None
    bokslut = (f'Bokslut {sie.year_ends[:6]} tom ver '
               f'{last_ver.series} {last_ver.number}') if last_ver else ''
    period_str = (f'{sie.year_begins[:4]}-{sie.year_begins[4:6]}-{sie.year_begins[6:]}'
                  f' – '
                  f'{sie.year_ends[:4]}-{sie.year_ends[4:6]}-{sie.year_ends[6:]}')
    for text in [bokslut, f'Räkenskapsår {period_str}']:
        tr = TableRow()
        tc = TableCell(stylename='meta', numbercolumnsspanned='4')
        tc.addElement(P(text=text))
        tr.addElement(tc)
        sheet.addElement(tr)

    _bal_empty_row(sheet)

    # Column headers
    ing_date  = sie.year_begins[2:]                    # e.g. 240101
    per_range = f'{sie.year_begins[2:]}-{sie.year_ends[2:]}'
    utg_date  = f'=>{sie.year_ends[2:]}'

    tr = TableRow()
    tr.addElement(_text_cell('', 'empty'))
    tr.addElement(_text_cell('ING BALANS',   'col_head'))
    tr.addElement(_text_cell('DENNA PERIOD', 'col_head'))
    tr.addElement(_text_cell('UTG SALDO',    'col_head'))
    sheet.addElement(tr)

    tr = TableRow()
    tr.addElement(_text_cell('', 'empty'))
    tr.addElement(_text_cell(ing_date,  'col_date'))
    tr.addElement(_text_cell(per_range, 'col_date'))
    tr.addElement(_text_cell(utg_date,  'col_date'))
    sheet.addElement(tr)

    _bal_empty_row(sheet)

    # ── helper: emit one subsection ────────────────────────────────────────
    def subsection(title: str, lo: int, hi: int, sum_title: str,
                   indent: str = '  ') -> tuple[Decimal, Decimal, Decimal]:
        rows = section_rows(lo, hi)
        si, sp, su = section_totals(lo, hi)
        if not rows and si == Z and sp == Z and su == Z:
            return Z, Z, Z
        tr = TableRow()
        tr.addElement(_text_cell(indent[:-2] + title if indent else title, 'subsect'))
        for _ in range(3):
            tr.addElement(_text_cell('', 'empty'))
        sheet.addElement(tr)
        for nr, lbl, iv, pv, uv in rows:
            _bal_account_row(sheet, f'{nr} {lbl}', iv, pv, uv, indent)
        _bal_subtotal_row(sheet, sum_title, si, sp, su)
        return si, sp, su

    # ── TILLGÅNGAR ─────────────────────────────────────────────────────────
    tr = TableRow()
    tr.addElement(_text_cell('TILLGÅNGAR', 'sect'))
    for _ in range(3):
        tr.addElement(_text_cell('', 'empty'))
    sheet.addElement(tr)

    # Anläggningstillgångar 1100-1399
    anl_i, anl_p, anl_u = subsection('Anläggningstillgångar', 1100, 1400,
                                      'Summa anläggningstillgångar')
    _bal_empty_row(sheet)

    # Omsättningstillgångar: contains sub-subsections
    oms_rows_exist = any(
        section_rows(lo, hi)
        for lo, hi in [(1400, 1500), (1500, 1800), (1800, 1900), (1900, 2000)]
    )
    if oms_rows_exist:
        tr = TableRow()
        tr.addElement(_text_cell('Omsättningstillgångar', 'subsect'))
        for _ in range(3):
            tr.addElement(_text_cell('', 'empty'))
        sheet.addElement(tr)

    var_i, var_p, var_u = subsection('Varulager', 1400, 1500,
                                     'Summa varulager', indent='    ')
    if var_i != Z or var_p != Z or var_u != Z:
        _bal_empty_row(sheet)

    fod_i, fod_p, fod_u = subsection('Fordringar', 1500, 1800,
                                      'Summa fordringar', indent='    ')
    if fod_i != Z or fod_p != Z or fod_u != Z:
        _bal_empty_row(sheet)

    plc_i, plc_p, plc_u = subsection('Kortfristiga placeringar', 1800, 1900,
                                      'Summa kortfristiga placeringar', indent='    ')
    if plc_i != Z or plc_p != Z or plc_u != Z:
        _bal_empty_row(sheet)

    kas_i, kas_p, kas_u = subsection('Kassa och bank', 1900, 2000,
                                      'Summa kassa och bank', indent='    ')

    oms_i = var_i + fod_i + plc_i + kas_i
    oms_p = var_p + fod_p + plc_p + kas_p
    oms_u = var_u + fod_u + plc_u + kas_u
    if oms_i != Z or oms_p != Z or oms_u != Z:
        _bal_empty_row(sheet)
        _bal_subtotal_row(sheet, 'Summa omsättningstillgångar',
                          oms_i, oms_p, oms_u)

    _bal_empty_row(sheet)
    tot_till_i = anl_i + oms_i
    tot_till_p = anl_p + oms_p
    tot_till_u = anl_u + oms_u
    _bal_subtotal_row(sheet, 'SUMMA TILLGÅNGAR',
                      tot_till_i, tot_till_p, tot_till_u, style_prefix='total')

    _bal_empty_row(sheet)

    # ── EGET OCH FRÄMMANDE KAPITAL ─────────────────────────────────────────
    tr = TableRow()
    tr.addElement(_text_cell('EGET OCH FRÄMMANDE KAPITAL', 'sect'))
    for _ in range(3):
        tr.addElement(_text_cell('', 'empty'))
    sheet.addElement(tr)

    EK_SECTIONS = [
        ('Eget kapital',         2000, 2100, 'Summa eget kapital'),
        ('Obeskattade reserver', 2100, 2200, 'Summa obeskattade reserver'),
        ('Avsättningar',         2200, 2300, 'Summa avsättningar'),
        ('Långfristiga skulder', 2300, 2400, 'Summa långfristiga skulder'),
        ('Kortfristiga skulder', 2400, 3000, 'Summa kortfristiga skulder'),
    ]

    tot_kap_i = tot_kap_p = tot_kap_u = Z
    for title, lo, hi, sum_title in EK_SECTIONS:
        si, sp, su = subsection(title, lo, hi, sum_title)
        tot_kap_i += si
        tot_kap_p += sp
        tot_kap_u += su
        if si != Z or sp != Z or su != Z:
            _bal_empty_row(sheet)

    # ── Beräknat resultat ─────────────────────────────────────────────────
    # Sum of all income/expense account transactions (3xxx–8xxx).
    # ING = 0 (P&L resets each year); DENNA = running YTD; UTG = same.
    ytd_p = sum(
        (v for k, v in period.items()
         if k.isdigit() and 3000 <= int(k) <= 8999),
        Z,
    )
    if ytd_p != Z:
        tr = TableRow()
        tr.addElement(_text_cell('Beräknat resultat', 'comp_lbl'))
        tr.addElement(_num_cell(_sek(Z),     'comp_num'))   # ING always 0
        tr.addElement(_num_cell(_sek(ytd_p), 'comp_num'))   # DENNA = YTD P&L
        tr.addElement(_num_cell(_sek(ytd_p), 'comp_num'))   # UTG = same
        sheet.addElement(tr)
        tot_kap_p += ytd_p
        tot_kap_u += ytd_p
        _bal_empty_row(sheet)

    _bal_subtotal_row(sheet, 'SUMMA EGET OCH FRÄMMANDE KAPITAL',
                      tot_kap_i, tot_kap_p, tot_kap_u, style_prefix='total')

    # ── footer ────────────────────────────────────────────────────────────
    _bal_empty_row(sheet)
    tr = TableRow()
    tc = TableCell(stylename='meta', numbercolumnsspanned='4')
    tc.addElement(P(text=f'Genererad {datetime.now().strftime("%Y-%m-%d %H:%M")}'))
    tr.addElement(tc)
    sheet.addElement(tr)

    doc.save(output_path)
