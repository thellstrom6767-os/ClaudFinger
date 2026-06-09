"""Generate and restore SIE 5 packages (.si5).

SIE 5 is an XML-based format packaged as a zip file:
  ledger_2024.si5 (zip)
  ├── sie5.xml          ← accounting data (XML, namespace http://www.sie.se/sie5)
  └── documents/        ← attached files referenced from the XML
      ├── Verifikation_A1.pdf
      └── ...

Specification reference: http://www.sie.se/sie5
Schema version targeted: SIE 5.0 (2019-11-18)

Round-trip note: SIE 5 does not carry #SRU codes, so those are lost
when restoring a SIE 5 file back to SIE 4 format.
"""
from __future__ import annotations

import base64
import io
import json
import tempfile
import os
import zipfile
from datetime import datetime
from decimal import Decimal
from typing import Optional
import xml.etree.ElementTree as ET

from .models import SIEFile
from . import underlag as underlag_module

SIE5_NS  = 'http://www.sie.se/sie5'
SIE5_TAG = f'{{{SIE5_NS}}}'      # shorthand: SIE5_TAG + 'Account' etc.

CONTENT_TYPES: dict[str, str] = {
    '.pdf':  'application/pdf',
    '.jpg':  'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.png':  'image/png',
    '.tif':  'image/tiff',
    '.tiff': 'image/tiff',
    '.xml':  'application/xml',
}


def _d(date_compact: str) -> str:
    """YYYYMMDD → YYYY-MM-DD"""
    d = date_compact
    return f'{d[:4]}-{d[4:6]}-{d[6:]}' if len(d) == 8 else d


def _amt(v: Decimal) -> str:
    return f'{v:.2f}'


def _account_type(number: str, ktyp: Optional[str]) -> str:
    if ktyp == 'T':
        return 'Asset'
    if ktyp == 'I':
        return 'Income'
    if ktyp == 'K':
        return 'Cost'
    if ktyp == 'S':
        return 'Liability'
    if number.isdigit():
        n = int(number)
        if 2000 <= n < 2100:
            return 'Equity'
        if 2100 <= n < 3000:
            return 'Liability'
    return 'Asset'


def _sub(parent: ET.Element, tag: str, **attrs) -> ET.Element:
    return ET.SubElement(parent, SIE5_TAG + tag, **attrs)


def _build_xml(
    sie: SIEFile,
    doc_refs: list[tuple[int, str]],   # [(doc_id, filename), ...]
    voucher_docs: dict[tuple[str,int], list[int]],  # (series,num) → [doc_id,...]
) -> bytes:
    ET.register_namespace('', SIE5_NS)
    root = ET.Element(SIE5_TAG + 'SIEDocument')

    # ── FileInfo ──────────────────────────────────────────────────────────
    fi = _sub(root, 'FileInfo')
    _sub(fi, 'SoftwareProduct', Name=sie.program, Version=sie.program_version)
    _sub(fi, 'FileCreation',
         Time=datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
         By=sie.gen_author)

    # Parse zip_city into PostalCode + City as best we can
    zip_parts = sie.zip_city.split(None, 1) if sie.zip_city else ['', '']
    postal = zip_parts[0] if zip_parts else ''
    city   = zip_parts[1] if len(zip_parts) > 1 else sie.zip_city

    co = _sub(fi, 'Company',
              OrganizationId=sie.org_nr.replace('-', ''),
              Name=sie.company_name)
    _sub(co, 'Address',
         Street=sie.street,
         PostalCode=postal,
         City=city,
         Country='SE')

    # ── FiscalYears ───────────────────────────────────────────────────────
    fy = _sub(root, 'FiscalYears')
    _sub(fy, 'FiscalYear',
         Primary='true',
         Start=_d(sie.year_begins),
         End=_d(sie.year_ends),
         AccountingCurrency=sie.currency)

    # ── AccountingPlan ────────────────────────────────────────────────────
    plan = _sub(root, 'AccountingPlan')
    for acc in sie.accounts:
        acc_el = _sub(plan, 'Account',
                      Id=acc.number,
                      Name=acc.label,
                      Type=_account_type(acc.number, acc.ktyp))
        if acc.number in sie.ib:
            _sub(acc_el, 'OpeningBalance', amount=_amt(sie.ib[acc.number]))
        if acc.number in sie.ub:
            _sub(acc_el, 'ClosingBalance', amount=_amt(sie.ub[acc.number]))
        if acc.number in sie.res:
            _sub(acc_el, 'ResultBalance', amount=_amt(sie.res[acc.number]))

    # ── Journals ──────────────────────────────────────────────────────────
    journals_el = _sub(root, 'Journals')

    series_map: dict[str, list] = {}
    for v in sie.vouchers:
        series_map.setdefault(v.series, []).append(v)

    for series_id, vouchers in sorted(series_map.items()):
        j = _sub(journals_el, 'Journal', Id=series_id, Name='')
        for v in vouchers:
            entry_attrs: dict[str, str] = {
                'Id':          str(v.number),
                'JournalDate': _d(v.date),
                'Text':        v.label,
            }
            if v.reg_date:
                entry_attrs['OriginalEntryDate'] = _d(v.reg_date)
            if v.signature:
                entry_attrs['CreatedBy'] = v.signature

            entry = _sub(j, 'JournalEntry', **entry_attrs)

            for t in v.transactions:
                t_attrs: dict[str, str] = {
                    'AccountId': t.account,
                    'Amount':    _amt(t.amount),
                }
                if t.label:
                    t_attrs['Text'] = t.label
                _sub(entry, 'LedgerEntry', **t_attrs)

            for doc_id in voucher_docs.get((v.series, v.number), []):
                _sub(entry, 'DocumentReference', DocumentId=str(doc_id))

    # ── Documents ─────────────────────────────────────────────────────────
    if doc_refs:
        docs_el = _sub(root, 'Documents')
        for doc_id, filename in doc_refs:
            ext  = os.path.splitext(filename)[1].lower()
            ctype = CONTENT_TYPES.get(ext, 'application/octet-stream')
            _sub(docs_el, 'Document',
                 Id=str(doc_id),
                 Name=filename,
                 ContentType=ctype)

    ET.indent(root, space='  ')
    buf = io.BytesIO()
    ET.ElementTree(root).write(buf, encoding='utf-8', xml_declaration=True)
    return buf.getvalue()


def _build_manifest(ledger_path: str, sie: SIEFile) -> bytes | None:
    """Build manifest.json content covering the hash chain and underlag hashes.

    Returns None when no chain entries exist (no hashing has been done yet).
    """
    from . import store as store_module

    all_entries = store_module.get_all_chain_entries(ledger_path)
    if not all_entries:
        return None

    chain_map: dict[tuple[str, int], dict] = {
        (e['series'], e['number']): e for e in all_entries
    }

    ib_entry = chain_map.get(('IB', 0))
    chains: dict[str, list] = {}

    for v in sie.vouchers:
        entry = chain_map.get((v.series, v.number))
        if entry is None:
            continue
        if v.number == 1:
            prev = chain_map.get(('IB', 0))
        else:
            prev = chain_map.get((v.series, v.number - 1))
        prev_hash = prev['hash'] if prev else None

        u_hashes = underlag_module.get_underlag_hashes(ledger_path, v.series, v.number)

        voucher_rec: dict = {
            'number':    v.number,
            'hash':      entry['hash'],
            'prev_hash': prev_hash,
        }
        if u_hashes:
            voucher_rec['underlag'] = [
                {'filename': fn, 'sha256': sha}
                for fn, sha in sorted(u_hashes.items())
            ]
        if entry['tsr_token']:
            voucher_rec['tsr_base64'] = base64.b64encode(entry['tsr_token']).decode()
        if entry['tsa_timestamp']:
            voucher_rec['tsr_time'] = entry['tsa_timestamp']

        chains.setdefault(v.series, []).append(voucher_rec)

    manifest: dict = {
        'version': 1,
        'ib': {'hash': ib_entry['hash']} if ib_entry else None,
        'chains': chains,
    }
    return json.dumps(manifest, indent=2, ensure_ascii=False).encode('utf-8')


def generate_sie5(
    sie: SIEFile,
    ledger_path: str,
    output_path: str,
) -> tuple[int, int]:
    """Write a SIE 5 zip package to output_path.

    Returns (n_vouchers, n_documents).
    """
    # ── collect underlag ──────────────────────────────────────────────────
    doc_counter = 0
    doc_entries: list[tuple[int, str, int]] = []     # (doc_id, filename, file_id)
    voucher_docs: dict[tuple[str, int], list[int]] = {}

    for summary in underlag_module.list_all(ledger_path):
        series, number = summary['series'], summary['number']
        for finfo in underlag_module.list_for_voucher(ledger_path, series, number):
            doc_counter += 1
            doc_entries.append((doc_counter, finfo['filename'], finfo['id']))
            voucher_docs.setdefault((series, number), []).append(doc_counter)

    doc_refs = [(doc_id, filename) for doc_id, filename, _ in doc_entries]

    # ── build XML then zip ────────────────────────────────────────────────
    xml_bytes = _build_xml(sie, doc_refs, voucher_docs)

    manifest_bytes = _build_manifest(ledger_path, sie)

    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('sie5.xml', xml_bytes)
        if manifest_bytes is not None:
            zf.writestr('manifest.json', manifest_bytes)
        for _doc_id, filename, file_id in doc_entries:
            data = underlag_module.get_data(ledger_path, file_id)
            if data:
                zf.writestr(f'documents/{filename}', data)

    return len(sie.vouchers), len(doc_refs)


# ─── restore ──────────────────────────────────────────────────────────────────

_TYPE_TO_KTYP: dict[str, str | None] = {
    'Asset':     'T',
    'Income':    'I',
    'Cost':      'K',
    'Liability': 'S',
    'Equity':    None,
}


def restore_from_sie5(
    si5_path: str,
    output_sie4_path: str,
) -> tuple[SIEFile, int]:
    """Restore a SIE 4 ledger + underlag from a SIE 5 package.

    Writes the SIE 4 file to output_sie4_path and populates the
    companion underlag store.  Returns (sie, n_documents_restored).
    """
    from .models import Account, Transaction, Voucher
    from . import underlag as ul

    ns = {'s': SIE5_NS}

    with zipfile.ZipFile(si5_path, 'r') as zf:
        zip_names = set(zf.namelist())
        root = ET.fromstring(zf.read('sie5.xml'))

        # ── FileInfo ──────────────────────────────────────────────────────
        fi  = root.find('s:FileInfo', ns)
        sp  = fi.find('s:SoftwareProduct', ns)  if fi is not None else None
        fc  = fi.find('s:FileCreation', ns)     if fi is not None else None
        co  = fi.find('s:Company', ns)           if fi is not None else None
        adr = co.find('s:Address', ns)           if co is not None else None

        program          = sp.get('Name', '')       if sp  is not None else ''
        program_version  = sp.get('Version', '')    if sp  is not None else ''
        gen_author       = fc.get('By', '')         if fc  is not None else ''
        gen_time         = fc.get('Time', '')        if fc  is not None else ''
        gen_date         = gen_time[:10].replace('-', '') if gen_time else ''
        company_name     = co.get('Name', '')       if co  is not None else ''
        org_nr_raw       = co.get('OrganizationId', '') if co is not None else ''
        org_nr = (f'{org_nr_raw[:6]}-{org_nr_raw[6:]}'
                  if len(org_nr_raw) == 10 else org_nr_raw)
        street   = adr.get('Street', '')      if adr is not None else ''
        postal   = adr.get('PostalCode', '')  if adr is not None else ''
        city     = adr.get('City', '')        if adr is not None else ''
        zip_city = f'{postal} {city}'.strip()

        # ── FiscalYear ────────────────────────────────────────────────────
        fy       = root.find('s:FiscalYears/s:FiscalYear', ns)
        year_beg = fy.get('Start', '').replace('-', '') if fy is not None else ''
        year_end = fy.get('End',   '').replace('-', '') if fy is not None else ''
        currency = fy.get('AccountingCurrency', 'SEK') if fy is not None else 'SEK'

        # ── AccountingPlan ────────────────────────────────────────────────
        accounts: list[Account] = []
        ib:  dict[str, Decimal] = {}
        ub:  dict[str, Decimal] = {}
        res: dict[str, Decimal] = {}

        for acc_el in root.findall('s:AccountingPlan/s:Account', ns):
            number = acc_el.get('Id', '')
            label  = acc_el.get('Name', '')
            ktyp   = _TYPE_TO_KTYP.get(acc_el.get('Type', ''))
            accounts.append(Account(number=number, label=label, ktyp=ktyp))

            ob = acc_el.find('s:OpeningBalance', ns)
            if ob is not None:
                ib[number] = Decimal(ob.get('amount', '0'))

            cb = acc_el.find('s:ClosingBalance', ns)
            if cb is not None:
                ub[number] = Decimal(cb.get('amount', '0'))

            rb = acc_el.find('s:ResultBalance', ns)
            if rb is not None:
                res[number] = Decimal(rb.get('amount', '0'))

        # ── Document manifest: id → filename ──────────────────────────────
        doc_manifest: dict[str, str] = {
            el.get('Id', ''): el.get('Name', '')
            for el in root.findall('s:Documents/s:Document', ns)
        }

        # ── Journals → vouchers ───────────────────────────────────────────
        vouchers: list[Voucher] = []
        voucher_doc_ids: dict[tuple[str, int], list[str]] = {}

        for journal in root.findall('s:Journals/s:Journal', ns):
            series = journal.get('Id', 'A')
            for entry in journal.findall('s:JournalEntry', ns):
                number   = int(entry.get('Id', '0'))
                v_date   = entry.get('JournalDate', '').replace('-', '')
                label    = entry.get('Text', '')
                reg_date = entry.get('OriginalEntryDate', '').replace('-', '')
                sig      = entry.get('CreatedBy', '')

                transactions = [
                    Transaction(
                        account=le.get('AccountId', ''),
                        amount =Decimal(le.get('Amount', '0')),
                        date   =v_date,
                        label  =le.get('Text', ''),
                    )
                    for le in entry.findall('s:LedgerEntry', ns)
                ]
                vouchers.append(Voucher(series=series, number=number,
                                        date=v_date, label=label,
                                        reg_date=reg_date, signature=sig,
                                        transactions=transactions))

                refs = [dr.get('DocumentId', '')
                        for dr in entry.findall('s:DocumentReference', ns)]
                if refs:
                    voucher_doc_ids[(series, number)] = refs

        # ── Build SIEFile and write to DB ────────────────────────────────
        sie = SIEFile(
            program=program, program_version=program_version,
            gen_date=gen_date, gen_author=gen_author,
            org_nr=org_nr, company_name=company_name,
            street=street, zip_city=zip_city,
            year_begins=year_beg, year_ends=year_end,
            currency=currency,
            accounts=accounts, ib=ib, ub=ub, res=res, vouchers=vouchers,
        )
        from .store import save_ledger as _save_ledger
        _save_ledger(output_sie4_path, sie)

        # ── Restore underlag ──────────────────────────────────────────────
        n_docs = 0
        if voucher_doc_ids and doc_manifest:
            with tempfile.TemporaryDirectory() as tmpdir:
                for filename in doc_manifest.values():
                    zip_entry = f'documents/{filename}'
                    if zip_entry in zip_names:
                        zf.extract(zip_entry, tmpdir)

                for (series, number), doc_ids in voucher_doc_ids.items():
                    for doc_id in doc_ids:
                        filename = doc_manifest.get(doc_id, '')
                        if not filename:
                            continue
                        src = os.path.join(tmpdir, 'documents', filename)
                        if not os.path.exists(src):
                            continue
                        ul.add_file(output_sie4_path, series, number, src)
                        n_docs += 1

        # ── Restore chain and TSR from manifest.json ──────────────────────
        if 'manifest.json' in zip_names:
            _restore_manifest(
                zf.read('manifest.json'), output_sie4_path
            )

    return sie, n_docs


def _restore_manifest(manifest_bytes: bytes, ledger_path: str) -> None:
    """Insert chain rows and TSR tokens from a manifest.json blob."""
    from . import store as store_module

    try:
        manifest = json.loads(manifest_bytes.decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return

    ib = manifest.get('ib')
    if ib and ib.get('hash'):
        store_module.insert_chain_entry(ledger_path, 'IB', 0, ib['hash'], replace=True)

    for series, entries in manifest.get('chains', {}).items():
        for rec in entries:
            number = rec.get('number')
            h = rec.get('hash', '')
            if not (number is not None and h):
                continue
            store_module.insert_chain_entry(ledger_path, series, number, h, replace=True)
            tsr_b64 = rec.get('tsr_base64')
            tsr_time = rec.get('tsr_time', '')
            if tsr_b64:
                try:
                    tsr_bytes = base64.b64decode(tsr_b64)
                except Exception:
                    continue
                store_module.update_chain_tsr(ledger_path, series, number, tsr_bytes, tsr_time)
