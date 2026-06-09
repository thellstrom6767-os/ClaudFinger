#!/usr/bin/env python3
"""SIE 5 hash-chain verifier.

Independent verifier for .si5 archives produced by bokforing.  Reads the
accounting data from sie5.xml and the attached document BLOBs from documents/,
then recomputes every SHA-256 hash in the chain *from scratch* — without
trusting manifest.json — and checks the results against what the manifest
records.  RFC 3161 timestamp tokens (TSR) stored in the manifest are verified
with openssl to confirm they stamp the independently computed hashes.

Two passes are run in sequence:

  PASS 1  Silent chain check.
          Produces one summary line per chain (IB root + one line per voucher
          series), reporting pass/fail and the tail-lock timestamp where
          applicable.  No preimages or intermediate values are printed.

  PASS 2  Full verbose audit trail.
          For every chain entry (IB and each voucher) the complete canonical
          preimage is printed verbatim.  UNDERLAG hash lines are annotated
          with the corresponding Verifikation filename.  Computed and manifest
          hashes are shown side-by-side.  TSR timestamps and their signature
          verification results are displayed.

Usage
-----
    python sie5verify.py ARCHIVE.si5

Requirements
------------
  Python ≥ 3.10  (standard library only — no third-party packages required)
  openssl         available on PATH — used for TSR signature verification
  certifi         (pip install certifi) — recommended CA bundle source;
                  falls back to common system CA paths if not installed

Exit codes
----------
  0  All checks passed (hashes match, all TSRs valid).
  1  One or more checks failed.
  2  Usage error or unreadable / incomplete archive.

Hash-chain format
-----------------
Each entry's preimage is a UTF-8 text string whose SHA-256 is the chain hash.

IB root (opening-balance root, anchors every series):

    PREV
    0000…0000         (64 zero hex digits — no real predecessor)

    IB
    YYYY              (four-digit fiscal year from year_begins)
    <account>:<amount.2f>   (sorted by account; non-zero balances only)
    …

Voucher (one per JournalEntry, within its series):

    PREV
    <sha256-hex of preceding chain entry>

    [UNDERLAG                    (section present only when files are attached)
    <sha256-hex of document 1>   (Verifikation filenames sorted alphabetically)
    <sha256-hex of document 2>
    ]                            (blank line terminates the section)
    VOUCHER
    <series>:<number>
    <YYYY-MM-DD voucher date>
    <YYYY-MM-DD registration date>
    <label / description>
    <account>:<amount.2f>    (sorted by account number; two decimal places)
    …

Every series is an independent chain rooted at the same IB hash.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from decimal import Decimal
from typing import Optional
import xml.etree.ElementTree as ET


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

SIE5_NS    = 'http://www.sie.se/sie5'
_NS        = {'s': SIE5_NS}
_ZERO_HASH = '0' * 64   # placeholder PREV for the IB root
_WIDTH     = 72          # column width for section rulers


# ─────────────────────────────────────────────────────────────────────────────
# Terminal colour helpers
# ─────────────────────────────────────────────────────────────────────────────

_USE_COLOUR = sys.stdout.isatty()


def _green(s: str) -> str:
    return f'\033[32m{s}\033[0m' if _USE_COLOUR else s


def _red(s: str) -> str:
    return f'\033[31m{s}\033[0m' if _USE_COLOUR else s


def _yellow(s: str) -> str:
    return f'\033[33m{s}\033[0m' if _USE_COLOUR else s


def _bold(s: str) -> str:
    return f'\033[1m{s}\033[0m' if _USE_COLOUR else s


def _tick(ok: Optional[bool]) -> str:
    if ok is True:
        return _green('✓')
    if ok is False:
        return _red('✗')
    return _yellow('?')


# ─────────────────────────────────────────────────────────────────────────────
# CA bundle discovery (for openssl TSR verification)
# ─────────────────────────────────────────────────────────────────────────────

def _find_ca_bundle() -> Optional[str]:
    """Return a path to a PEM CA bundle suitable for openssl, or None.

    Search order:
      1. certifi (pip install certifi) — Mozilla bundle, reliably up to date.
      2. Common system paths on Debian/Ubuntu, RHEL/CentOS, and macOS.
    """
    try:
        import certifi  # type: ignore
        return certifi.where()
    except ImportError:
        pass
    for path in (
        '/etc/ssl/certs/ca-certificates.crt',   # Debian / Ubuntu
        '/etc/pki/tls/certs/ca-bundle.crt',     # RHEL / CentOS
        '/etc/ssl/cert.pem',                    # macOS / Alpine
    ):
        if os.path.exists(path):
            return path
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Date helper
# ─────────────────────────────────────────────────────────────────────────────

def _iso(date_compact: str) -> str:
    """Convert YYYYMMDD → YYYY-MM-DD.  Already-formatted strings pass through."""
    d = date_compact.replace('-', '')
    if len(d) == 8 and d.isdigit():
        return f'{d[:4]}-{d[4:6]}-{d[6:]}'
    return date_compact


# ─────────────────────────────────────────────────────────────────────────────
# Parsing — sie5.xml
# ─────────────────────────────────────────────────────────────────────────────

def parse_sie5_xml(xml_bytes: bytes) -> dict:
    """Parse sie5.xml and return all data required for chain verification.

    Returned dict keys
    ------------------
    year_begins   str                YYYYMMDD (first day of the fiscal year)
    ib            dict[str,Decimal]  account → opening balance (non-zero only)
    vouchers      list[dict]         in document order (not yet sorted by series/number)
    doc_manifest  dict[str,str]      document_id → Verifikation_* filename

    Each voucher dict contains
    --------------------------
    series    str
    number    int
    date      str    YYYYMMDD
    reg_date  str    YYYYMMDD (empty string if absent)
    label     str
    trans     list[dict]   each with 'account' (str) and 'amount' (Decimal)
    doc_ids   list[str]    DocumentReference IDs, in element order
    """
    root = ET.fromstring(xml_bytes)

    # Fiscal year — Start attribute of the primary FiscalYear element
    fy = root.find('s:FiscalYears/s:FiscalYear', _NS)
    year_begins = (fy.get('Start', '') if fy is not None else '').replace('-', '')

    # Opening balances — non-zero entries from AccountingPlan/Account/OpeningBalance
    ib: dict[str, Decimal] = {}
    for acc in root.findall('s:AccountingPlan/s:Account', _NS):
        ob = acc.find('s:OpeningBalance', _NS)
        if ob is not None:
            amount = Decimal(ob.get('amount', '0'))
            if amount != Decimal('0'):
                ib[acc.get('Id', '')] = amount

    # Document manifest — numeric Id → Verifikation filename
    doc_manifest: dict[str, str] = {
        doc.get('Id', ''): doc.get('Name', '')
        for doc in root.findall('s:Documents/s:Document', _NS)
    }

    # Vouchers — one dict per JournalEntry across all Journal elements
    vouchers: list[dict] = []
    for journal in root.findall('s:Journals/s:Journal', _NS):
        series = journal.get('Id', 'A')
        for entry in journal.findall('s:JournalEntry', _NS):
            vouchers.append({
                'series':   series,
                'number':   int(entry.get('Id', '0')),
                'date':     entry.get('JournalDate',       '').replace('-', ''),
                'reg_date': entry.get('OriginalEntryDate', '').replace('-', ''),
                'label':    entry.get('Text', ''),
                'trans': [
                    {
                        'account': le.get('AccountId', ''),
                        'amount':  Decimal(le.get('Amount', '0')),
                    }
                    for le in entry.findall('s:LedgerEntry', _NS)
                ],
                'doc_ids': [
                    dr.get('DocumentId', '')
                    for dr in entry.findall('s:DocumentReference', _NS)
                ],
            })

    return {
        'year_begins':  year_begins,
        'ib':           ib,
        'vouchers':     vouchers,
        'doc_manifest': doc_manifest,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Parsing — manifest.json
# ─────────────────────────────────────────────────────────────────────────────

def parse_manifest(manifest_bytes: bytes) -> dict:
    """Parse manifest.json and return a normalised verification dict.

    Returned dict keys
    ------------------
    ib_hash   str | None        SHA-256 hex recorded for the IB root
    chains    dict[str, list]   series → list of entry dicts (ascending number)

    Each entry dict in chains[series]
    ----------------------------------
    number      int
    hash        str          SHA-256 hex recorded in the manifest
    prev_hash   str | None   hash of the preceding chain entry
    underlag    list[dict]   each with 'filename' (str) and 'sha256' (str)
    tsr_base64  str | None   base64-encoded RFC 3161 TSR blob
    tsr_time    str | None   UTC ISO 8601 timestamp extracted from the TSR
    """
    raw = json.loads(manifest_bytes.decode('utf-8'))

    ib_section = raw.get('ib') or {}
    ib_hash: Optional[str] = ib_section.get('hash')

    chains: dict[str, list] = {}
    for series, entries in raw.get('chains', {}).items():
        chains[series] = [
            {
                'number':     rec.get('number'),
                'hash':       rec.get('hash', ''),
                'prev_hash':  rec.get('prev_hash'),
                'underlag':   rec.get('underlag', []),
                'tsr_base64': rec.get('tsr_base64'),
                'tsr_time':   rec.get('tsr_time'),
            }
            for rec in entries
        ]

    return {'ib_hash': ib_hash, 'chains': chains}


# ─────────────────────────────────────────────────────────────────────────────
# Canonical preimage construction
# ─────────────────────────────────────────────────────────────────────────────

def canonical_ib_text(year_begins: str, ib: dict[str, Decimal]) -> str:
    """Build the canonical UTF-8 preimage for the IB root hash entry.

    The IB root anchors every voucher series chain.  Because it has no
    predecessor it uses a 64-character all-zeros string as PREV.

    Format:
        PREV
        0000…0000

        IB
        YYYY
        <account>:<amount.2f>   (sorted by account; non-zero balances only)
        …
    """
    year = year_begins[:4]
    lines = ['PREV', _ZERO_HASH, '', 'IB', year]
    for account, amount in sorted(ib.items()):
        if amount != Decimal('0'):
            lines.append(f'{account}:{amount:.2f}')
    return '\n'.join(lines) + '\n'


def canonical_voucher_text(
    voucher: dict,
    prev_hash: str,
    underlag_hashes: dict[str, str],
) -> str:
    """Build the canonical UTF-8 preimage for a voucher's chain hash entry.

    underlag_hashes maps each attached document's Verifikation filename to
    its SHA-256 hex digest.  Filenames are sorted before insertion so the
    result is deterministic regardless of the order they were added to the DB.

    Format:
        PREV
        <sha256-hex of preceding chain entry>

        [UNDERLAG                          (omitted when no files attached)
        <sha256-hex of document 1>
        <sha256-hex of document 2>         (alphabetical Verifikation order)
        ]                                  (blank line ends the UNDERLAG block)
        VOUCHER
        <series>:<number>
        <YYYY-MM-DD voucher date>
        <YYYY-MM-DD registration date>
        <label>
        <account>:<amount.2f>              (sorted by account number)
        …
    """
    lines = ['PREV', prev_hash, '']
    if underlag_hashes:
        lines.append('UNDERLAG')
        for filename in sorted(underlag_hashes):
            lines.append(underlag_hashes[filename])
        lines.append('')
    lines += [
        'VOUCHER',
        f'{voucher["series"]}:{voucher["number"]}',
        _iso(voucher['date']),
        _iso(voucher['reg_date']),
        voucher['label'],
    ]
    for t in sorted(voucher['trans'], key=lambda x: x['account']):
        lines.append(f'{t["account"]}:{t["amount"]:.2f}')
    return '\n'.join(lines) + '\n'


# ─────────────────────────────────────────────────────────────────────────────
# Supporting-document hashing and manifest cross-check
# ─────────────────────────────────────────────────────────────────────────────

def build_underlag_hashes(
    voucher: dict,
    doc_manifest: dict[str, str],
    zf: zipfile.ZipFile,
) -> dict[str, str]:
    """SHA-256 every document attached to voucher by reading it from the zip.

    Resolves each DocumentReference Id through doc_manifest to a
    Verifikation_* filename, reads documents/<filename> from the zip, and
    returns {filename: sha256_hex}.

    The returned dict is keyed by the Verifikation filename — exactly what
    canonical_voucher_text expects.  Missing zip entries are skipped silently
    (the hash comparison will fail, surfacing the problem).
    """
    hashes: dict[str, str] = {}
    for doc_id in voucher['doc_ids']:
        filename = doc_manifest.get(doc_id, '')
        if not filename:
            continue
        try:
            data = zf.read(f'documents/{filename}')
        except KeyError:
            continue
        hashes[filename] = hashlib.sha256(data).hexdigest()
    return hashes


def check_underlag_vs_manifest(
    computed: dict[str, str],
    manifest_entries: list[dict],
) -> list[dict]:
    """Cross-check computed underlag hashes against manifest entries.

    computed         is the dict returned by build_underlag_hashes:
                     {Verifikation_filename: sha256_hex}
    manifest_entries is the 'underlag' list from the manifest voucher record:
                     [{'filename': str, 'sha256': str}, …]

    Returns one record per filename appearing in either source, sorted
    alphabetically.  Each record:
        filename   str
        computed   str | None    SHA-256 derived from the document BLOB in the zip
        manifest   str | None    SHA-256 recorded in manifest.json
        ok         bool          True iff both sides are present and equal

    A missing-from-either-side entry is always reported as not ok, so callers
    can surface documents that were added or removed after the manifest was
    generated.
    """
    manifest_map = {e['filename']: e['sha256'] for e in manifest_entries}
    all_filenames = sorted(set(computed.keys()) | set(manifest_map.keys()))
    return [
        {
            'filename': fn,
            'computed': computed.get(fn),
            'manifest': manifest_map.get(fn),
            'ok': (
                computed.get(fn) is not None
                and manifest_map.get(fn) is not None
                and computed.get(fn) == manifest_map.get(fn)
            ),
        }
        for fn in all_filenames
    ]


# ─────────────────────────────────────────────────────────────────────────────
# RFC 3161 TSR verification and certificate detail extraction
# ─────────────────────────────────────────────────────────────────────────────

def _parse_first_cert(text: str) -> dict:
    """Parse the first X.509 block from ``openssl pkcs7 -text`` output.

    OpenSSL formats the same field differently depending on version and flags:
      - ``subject=C = US, O = ...``  (one-liner outside the cert block)
      - ``Subject: C=US, O=...``     (indented inside the Certificate block)

    Both are handled.  The first occurrence of each field wins, so we always
    capture the leaf (signing) certificate rather than a CA in the chain.

    Returned keys (all str; absent when not found):
        subject     Subject DN of the TSA leaf certificate
        issuer      Issuer DN
        not_before  Validity start as printed by openssl
        not_after   Validity end
    """
    info: dict[str, str] = {}
    for line in text.splitlines():
        s = line.strip()
        # subject — either "subject=..." (one-liner) or "Subject: ..." (in cert block)
        if s.startswith('subject=') and 'subject' not in info:
            info['subject'] = s[len('subject='):].strip()
        elif s.startswith('Subject:') and 'subject' not in info:
            info['subject'] = s[len('Subject:'):].strip()
        # issuer
        elif s.startswith('issuer=') and 'issuer' not in info:
            info['issuer'] = s[len('issuer='):].strip()
        elif s.startswith('Issuer:') and 'issuer' not in info:
            info['issuer'] = s[len('Issuer:'):].strip()
        # validity
        elif s.startswith('Not Before:') and 'not_before' not in info:
            info['not_before'] = s.split(':', 1)[1].strip()
        elif s.startswith('Not After :') and 'not_after' not in info:
            info['not_after'] = s.split(':', 1)[1].strip()
        if len(info) == 4:
            break
    return info


def _parse_stamped_hash(ts_text: str) -> Optional[str]:
    """Extract the messageImprint hash hex from ``openssl ts -reply -text`` output.

    The TSTInfo block contains a hex dump of the hash that the TSA signed:

        Message data:
            0000 - 63:53:f9:69:0c:de:5c:fe-fb:0e:b7:3a:8e:82:5a:94-
            0010 - 7a:bf:97:fc:bf:7c:14:bb-98:e8:18:85:62:72:65:98-

    Each data line starts with a hex offset followed by `` - ``.  All
    hexadecimal characters after that separator are concatenated to form the
    full hash string, ignoring colons and dashes used as visual separators.
    """
    in_block = False
    hex_chars: list[str] = []
    for line in ts_text.splitlines():
        s = line.strip()
        if s.startswith('Message data:'):
            in_block = True
            continue
        if in_block:
            # Data lines: "XXXX - xx:xx:xx..."
            if s and s[0] in '0123456789abcdefABCDEF' and ' - ' in s:
                after = s.split(' - ', 1)[1]
                hex_chars.extend(c for c in after if c in '0123456789abcdef')
            else:
                break   # first non-data line ends the block
    return ''.join(hex_chars) if hex_chars else None


def extract_tsr_details(tsr_bytes: bytes) -> dict:
    """Extract the stamped hash and signing-certificate details from an RFC 3161 TSR.

    Three openssl operations are run against the same temp file:

      1. ``openssl ts -reply -text`` — prints TSTInfo in human-readable form;
         the ``Message data:`` hex dump is the messageImprint (stamped hash).
      2. ``openssl ts -reply -token_out`` — unwraps the TSR envelope and emits
         the raw CMS/PKCS#7 token on stdout.
      3. ``openssl pkcs7 -print_certs -text -noout`` — lists the certificates
         embedded in the token; the first is the TSA leaf (signing) cert.

    Returned dict keys (all str; absent when openssl is unavailable or parsing fails):
        stamped_hash   hex string of the messageImprint extracted from the TSR
        subject        Subject DN of the TSA leaf certificate
        issuer         Issuer DN
        not_before     Validity start as printed by openssl
        not_after      Validity end
    """
    with tempfile.NamedTemporaryFile(suffix='.tsr', delete=False) as f:
        f.write(tsr_bytes)
        tsr_path = f.name
    try:
        info: dict = {}

        # Step 1 — messageImprint (stamped hash) from TSTInfo text
        r_text = subprocess.run(
            ['openssl', 'ts', '-reply', '-in', tsr_path, '-text'],
            capture_output=True, text=True,
        )
        stamped = _parse_stamped_hash(r_text.stdout)
        if stamped:
            info['stamped_hash'] = stamped

        # Step 2 — raw CMS token
        r1 = subprocess.run(
            ['openssl', 'ts', '-reply', '-in', tsr_path, '-token_out'],
            capture_output=True,
        )
        if r1.returncode == 0 and r1.stdout:
            # Step 3 — certificate details from the token
            r2 = subprocess.run(
                ['openssl', 'pkcs7', '-inform', 'DER', '-print_certs', '-text', '-noout'],
                input=r1.stdout, capture_output=True,
            )
            info.update(_parse_first_cert(r2.stdout.decode('utf-8', errors='replace')))

        return info
    except FileNotFoundError:
        return {}
    finally:
        os.unlink(tsr_path)


def verify_tsr(
    tsr_bytes: bytes,
    hash_hex: str,
    ca_bundle: Optional[str],
) -> tuple[bool, str]:
    """Verify an RFC 3161 TimeStampResponse against a known SHA-256 hash.

    Uses ``openssl ts -verify -digest`` which checks two things:
      1. The TSR was signed by a CA in ca_bundle (chain trust).
      2. The messageImprint inside the TSR matches hash_hex exactly.

    The nonce from the original TimeStampRequest is NOT checked because the
    request is unavailable at verification time; -digest mode skips it.

    Passing the *independently computed* hash (not the manifest hash) here
    confirms that the TSR stamps the data we verified ourselves — not just
    whatever the manifest claims.

    Returns (ok: bool, message: str).
    """
    if ca_bundle is None:
        return False, 'no CA bundle found — install certifi or check /etc/ssl'

    with tempfile.NamedTemporaryFile(suffix='.tsr', delete=False) as f:
        f.write(tsr_bytes)
        tsr_path = f.name
    try:
        result = subprocess.run(
            ['openssl', 'ts', '-verify',
             '-digest', hash_hex,
             '-in',     tsr_path,
             '-CAfile', ca_bundle],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return True, 'signature valid'
        # openssl writes the failure reason to stderr
        detail = (result.stderr or result.stdout).strip().splitlines()[-1]
        return False, f'INVALID — {detail}'
    except FileNotFoundError:
        return False, 'openssl not found in PATH — cannot verify TSR signatures'
    finally:
        os.unlink(tsr_path)


# ─────────────────────────────────────────────────────────────────────────────
# Core computation (runs once; drives both passes)
# ─────────────────────────────────────────────────────────────────────────────

def compute_verification(
    ledger: dict,
    manifest: dict,
    zf: zipfile.ZipFile,
    ca_bundle: Optional[str],
) -> dict:
    """Recompute every chain hash from sie5.xml data and verify against manifest.

    The computation proceeds identically to the original bokforing chain:
      1. Compute the IB root hash from opening balances.
      2. For each series (sorted), walk vouchers in ascending number order.
         prev_hash starts at the IB hash and advances entry by entry.
      3. For each voucher, compute underlag hashes from the document BLOBs,
         build the canonical preimage, hash it, compare to manifest, and
         verify any TSR against the computed (not manifest) hash.

    Returned dict keys
    ------------------
    ib      dict    IB result — see keys below
    series  dict    series_id → list of voucher result dicts

    IB result keys:
        preimage      canonical preimage string
        computed      str   SHA-256 of preimage
        manifest_hash str | None
        ok            bool
        covered_by    list[{series, number, tsr_time, tsr_ok}]
                      every series' first lock point (all cover IB because IB
                      is the root of every chain)

    Voucher result keys:
        voucher          original dict from parse_sie5_xml
        preimage         canonical preimage string
        underlag         dict[filename, sha256]  (computed from zip)
        underlag_checks  list[{filename, computed, manifest, ok}]
        underlag_ok      bool   True iff all underlag hashes match (or none present)
        computed         str   SHA-256 of preimage
        manifest_hash    str | None
        hash_ok          bool
        tsr_time         str | None
        tsr_ok           bool | None  (None = no TSR present)
        tsr_msg          str | None
        tsr_info         dict   TSA cert details: subject, issuer, not_before, not_after
                                (empty dict when no TSR or openssl unavailable)
        covered_by       {series, number, tsr_time, tsr_ok} | None
                         earliest lock in the same series that covers this entry
                         (the first entry at index >= this one that has a TSR);
                         None if the entry and all subsequent entries are unlocked
    """
    ib_preimage = canonical_ib_text(ledger['year_begins'], ledger['ib'])
    ib_computed = hashlib.sha256(ib_preimage.encode('utf-8')).hexdigest()
    ib_manifest = manifest['ib_hash']

    result: dict = {
        'ib': {
            'preimage':      ib_preimage,
            'computed':      ib_computed,
            'manifest_hash': ib_manifest,
            'ok':            ib_computed == ib_manifest,
        },
        'series': {},
    }

    # Group vouchers by series, sort each group by number
    series_map: dict[str, list[dict]] = {}
    for v in ledger['vouchers']:
        series_map.setdefault(v['series'], []).append(v)
    for s in series_map:
        series_map[s].sort(key=lambda v: v['number'])

    chains_manifest = manifest['chains']

    for series_id in sorted(series_map.keys()):
        manifest_by_num: dict[int, dict] = {
            e['number']: e
            for e in chains_manifest.get(series_id, [])
        }
        prev_hash      = ib_computed   # every series roots at the IB hash
        series_results: list[dict] = []

        for v in series_map[series_id]:
            underlag = build_underlag_hashes(v, ledger['doc_manifest'], zf)
            preimage = canonical_voucher_text(v, prev_hash, underlag)
            computed = hashlib.sha256(preimage.encode('utf-8')).hexdigest()

            mentry        = manifest_by_num.get(v['number'])
            manifest_hash = mentry['hash'] if mentry else None
            hash_ok       = computed == manifest_hash

            # Underlag cross-check — compare hashes computed from the zip BLOBs
            # against the hashes recorded in manifest.json for this voucher.
            manifest_underlag = mentry['underlag'] if mentry else []
            underlag_checks   = check_underlag_vs_manifest(underlag, manifest_underlag)
            underlag_ok       = all(c['ok'] for c in underlag_checks) if underlag_checks else True

            # TSR — verify against the *computed* hash so we confirm the TSR
            # stamps the data we independently derived, not just the manifest's claim
            tsr_ok   = None
            tsr_time = None
            tsr_msg  = None
            tsr_info: dict = {}
            if mentry and mentry.get('tsr_base64'):
                tsr_time = mentry.get('tsr_time')
                try:
                    tsr_bytes = base64.b64decode(mentry['tsr_base64'])
                except Exception as exc:
                    tsr_ok  = False
                    tsr_msg = f'cannot decode tsr_base64: {exc}'
                else:
                    tsr_ok, tsr_msg = verify_tsr(tsr_bytes, computed, ca_bundle)
                    tsr_info = extract_tsr_details(tsr_bytes)

            series_results.append({
                'voucher':          v,
                'preimage':         preimage,
                'underlag':         underlag,
                'underlag_checks':  underlag_checks,
                'underlag_ok':      underlag_ok,
                'computed':         computed,
                'manifest_hash':    manifest_hash,
                'hash_ok':          hash_ok,
                'tsr_time':         tsr_time,
                'tsr_ok':           tsr_ok,
                'tsr_msg':          tsr_msg,
                'tsr_info':         tsr_info,
            })

            prev_hash = computed   # advance the chain regardless of match status

        result['series'][series_id] = series_results

    # Post-pass: annotate each voucher with the earliest lock that covers it.
    # Because the chain is sequential, the TSR at position N covers entries 1..N.
    # We walk forward from each entry to find the first TSR at index >= its own.
    for series_id, entries in result['series'].items():
        for i, e in enumerate(entries):
            e['covered_by'] = None
            for future_e in entries[i:]:
                if future_e['tsr_time']:
                    v = future_e['voucher']
                    e['covered_by'] = {
                        'series':   series_id,
                        'number':   v['number'],
                        'tsr_time': future_e['tsr_time'],
                        'tsr_ok':   future_e['tsr_ok'],
                    }
                    break

    # IB is covered by the first lock in every series (each chain roots at IB).
    result['ib']['covered_by'] = []
    for series_id, entries in result['series'].items():
        for e in entries:
            if e['tsr_time']:
                result['ib']['covered_by'].append({
                    'series':   series_id,
                    'number':   e['voucher']['number'],
                    'tsr_time': e['tsr_time'],
                    'tsr_ok':   e['tsr_ok'],
                })
                break

    return result


def _result_ok(result: dict) -> bool:
    """Return True iff every hash matched, every underlag cross-check passed, and every TSR (if present) was valid."""
    if not result['ib']['ok']:
        return False
    for entries in result['series'].values():
        for e in entries:
            if not e['hash_ok']:
                return False
            if not e['underlag_ok']:
                return False
            if e['tsr_ok'] is False:
                return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Pass 1 — terse output
# ─────────────────────────────────────────────────────────────────────────────

def render_pass1(result: dict) -> None:
    """Print one summary line per chain entry (IB root + one line per series)."""
    print(_bold('Pass 1 — chain verification'))
    print()

    # IB root
    ib = result['ib']
    if ib['ok']:
        ib_status = _green('✓')
    else:
        got  = (ib['manifest_hash'] or 'missing')[:16] + '…'
        want = ib['computed'][:16] + '…'
        ib_status = _red(f'✗  computed {want}  manifest {got}')
    print(f'  Verifying IB root … {ib_status}')

    # Per-series
    for series_id, entries in sorted(result['series'].items()):
        n = len(entries)

        hash_failures     = [e for e in entries if not e['hash_ok']]
        underlag_failures = [e for e in entries if not e['underlag_ok']]
        tsr_failures      = [e for e in entries if e['tsr_ok'] is False]
        series_ok         = not hash_failures and not underlag_failures and not tsr_failures

        # TSR lock-point summary — only shown on a fully-passing series so the
        # "locked" note is never displayed alongside a failure message.
        tsr_entries = [e for e in entries if e['tsr_time']]
        tsr_note = ''
        if series_ok and tsr_entries:
            tail_time = tsr_entries[-1]['tsr_time']
            if len(tsr_entries) == 1:
                tsr_note = f'  locked {tail_time}'
            else:
                tsr_note = f'  {len(tsr_entries)} timestamps, last locked {tail_time}'

        if series_ok:
            status = _green('✓')
        else:
            parts = []
            if hash_failures:
                refs = ', '.join(
                    f'{series_id}:{e["voucher"]["number"]}' for e in hash_failures
                )
                parts.append(f'hash mismatch at {refs}')
            if underlag_failures:
                refs = ', '.join(
                    f'{series_id}:{e["voucher"]["number"]}' for e in underlag_failures
                )
                parts.append(f'underlag mismatch at {refs}')
            if tsr_failures:
                refs = ', '.join(
                    f'{series_id}:{e["voucher"]["number"]}' for e in tsr_failures
                )
                parts.append(f'TSR invalid at {refs}')
            status = _red('✗  ' + '; '.join(parts))

        n_str = f'{n} voucher{"s" if n != 1 else ""}'
        print(f'  Verifying chain for series {series_id} … {status}  {n_str}{tsr_note}')

    print()


# ─────────────────────────────────────────────────────────────────────────────
# Pass 2 — verbose output helpers
# ─────────────────────────────────────────────────────────────────────────────

_RULE = '─' * _WIDTH


def _section(label: str) -> None:
    print()
    print(_RULE)
    print(_bold(label))
    print(_RULE)


def _print_preimage_annotated(preimage: str, sha_to_filename: dict[str, str]) -> None:
    """Print the preimage indented, annotating UNDERLAG hash lines with filenames."""
    print('  Preimage:')
    for line in preimage.rstrip('\n').splitlines():
        # A 64-character lowercase hex string on its own line is an underlag hash
        stripped = line.strip()
        annotation = ''
        if stripped in sha_to_filename:
            annotation = _yellow(f'   ← {sha_to_filename[stripped]}')
        print(f'    {line}{annotation}')
    print()


def _print_hash_comparison(computed: str, manifest_hash: Optional[str], ok: bool) -> None:
    print(f'  SHA-256 (computed):   {computed}')
    if manifest_hash is None:
        print(f'  SHA-256 (manifest):   {_red("(not in manifest)")}  {_tick(False)}')
    elif ok:
        print(f'  SHA-256 (manifest):   {manifest_hash}  {_tick(True)}')
    else:
        print(f'  SHA-256 (manifest):   {_red(manifest_hash)}  {_tick(False)}')


def _print_underlag_checks(checks: list[dict]) -> None:
    """Print underlag cross-check results (computed-from-zip vs manifest.json).

    Only prints the block when there are underlag files to report.  For each
    file, one summary line is printed; on mismatch the two hashes are shown
    on separate indented lines.
    """
    if not checks:
        return
    print('  Underlag (computed from zip vs manifest):')
    for c in checks:
        tick = _tick(c['ok'])
        if c['ok']:
            print(f'    {tick}  {c["filename"]}')
        else:
            comp = c['computed'] if c['computed'] else _red('(missing)')
            mani = c['manifest'] if c['manifest'] else _red('(missing)')
            print(f'    {tick}  {c["filename"]}')
            if c['computed'] != c['manifest']:
                print(f'         computed:  {comp}')
                print(f'         manifest:  {mani}')


def _print_tsr(
    tsr_time: Optional[str],
    tsr_ok: Optional[bool],
    tsr_msg: Optional[str],
    computed: str,
    tsr_info: dict,
) -> None:
    """Print the lock-point timestamp and TSA certificate details for one entry."""
    if tsr_time is None:
        return
    sig_display = tsr_msg or ''
    print(f'  Locked here:          {tsr_time}  {_tick(tsr_ok)}  {sig_display}')
    stamped = tsr_info.get('stamped_hash', '')
    if stamped:
        print(f'  TSA stamped hash:     {stamped[:32]}…  (extracted from TSR)')
    else:
        print(f'  TSA stamped hash:     (could not extract from TSR)')
    if tsr_info.get('subject'):
        print(f'  TSA subject:          {tsr_info["subject"]}')
    if tsr_info.get('issuer'):
        print(f'  TSA issuer:           {tsr_info["issuer"]}')
    if tsr_info.get('not_before') and tsr_info.get('not_after'):
        print(f'  TSA cert valid:       {tsr_info["not_before"]}  –  {tsr_info["not_after"]}')


def _print_covered_by(covered_by_list: list[dict]) -> None:
    """Print one coverage line per lock that protects this chain entry.

    An entry without its own TSR is covered by the earliest subsequent lock in
    the same series.  IB is covered by all series' first locks.  Each line shows
    where the covering TSR lives (series:number) and its timestamp.
    """
    for cb in covered_by_list:
        ref = f'{cb["series"]}:{cb["number"]}'
        print(f'  Covered by lock:      {ref}  {cb["tsr_time"]}  {_tick(cb["tsr_ok"])}')


# ─────────────────────────────────────────────────────────────────────────────
# Pass 2 — verbose output
# ─────────────────────────────────────────────────────────────────────────────

def render_pass2(result: dict) -> None:
    """Print the complete audit trail with preimages, hashes, and TSR details."""
    print(_bold('Pass 2 — verbose audit trail'))

    # ── IB root ──────────────────────────────────────────────────────────────
    _section('IB  (opening-balance root — anchors all series)')
    ib = result['ib']
    _print_preimage_annotated(ib['preimage'], {})
    _print_hash_comparison(ib['computed'], ib['manifest_hash'], ib['ok'])
    _print_covered_by(ib.get('covered_by', []))

    # ── Per-series vouchers ───────────────────────────────────────────────────
    for series_id, entries in sorted(result['series'].items()):
        for e in entries:
            v   = e['voucher']
            ref = f'{series_id}:{v["number"]}'

            _section(ref)

            # Build sha256 → filename map for UNDERLAG annotation
            sha_to_filename: dict[str, str] = {
                sha: fn for fn, sha in e['underlag'].items()
            }

            _print_preimage_annotated(e['preimage'], sha_to_filename)
            _print_hash_comparison(e['computed'], e['manifest_hash'], e['hash_ok'])
            _print_underlag_checks(e['underlag_checks'])
            _print_tsr(e['tsr_time'], e['tsr_ok'], e['tsr_msg'], e['computed'], e.get('tsr_info', {}))
            if e['tsr_time'] is None:
                _print_covered_by([e['covered_by']] if e['covered_by'] else [])

    print()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description='Verify the hash chain and TSR tokens in a SIE 5 .si5 archive.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            'Pass 1 prints one summary line per series.\n'
            'Pass 2 prints the full preimage, hashes, and TSR details for every entry.\n'
            'Exit code 0 = all checks passed; 1 = one or more failures; 2 = usage error.'
        ),
    )
    parser.add_argument('archive', metavar='ARCHIVE.si5',
                        help='SIE 5 zip archive to verify')
    args = parser.parse_args()

    si5_path = args.archive
    if not os.path.exists(si5_path):
        print(f'Error: file not found: {si5_path}', file=sys.stderr)
        sys.exit(2)

    # Discover CA bundle once for all TSR verifications
    ca_bundle = _find_ca_bundle()
    if ca_bundle is None:
        print(
            _yellow('Warning: no CA bundle found — TSR signatures cannot be verified.\n'
                    '         Install certifi (pip install certifi) to enable TSR checks.'),
            file=sys.stderr,
        )

    # ── Open and parse archive ────────────────────────────────────────────────
    try:
        zf = zipfile.ZipFile(si5_path, 'r')
    except (zipfile.BadZipFile, OSError) as exc:
        print(f'Error: cannot open archive: {exc}', file=sys.stderr)
        sys.exit(2)

    with zf:
        zip_names = set(zf.namelist())

        if 'sie5.xml' not in zip_names:
            print('Error: sie5.xml not found in archive', file=sys.stderr)
            sys.exit(2)
        if 'manifest.json' not in zip_names:
            print('Error: manifest.json not found — archive has no hash chain data',
                  file=sys.stderr)
            sys.exit(2)

        try:
            ledger   = parse_sie5_xml(zf.read('sie5.xml'))
            manifest = parse_manifest(zf.read('manifest.json'))
        except Exception as exc:
            print(f'Error: failed to parse archive contents: {exc}', file=sys.stderr)
            sys.exit(2)

        print(f'Verifying: {si5_path}')
        if ledger['year_begins']:
            year = ledger['year_begins'][:4]
            print(f'Fiscal year: {year}')
        print()

        # ── Single computation pass, two rendering passes ─────────────────────
        result = compute_verification(ledger, manifest, zf, ca_bundle)

    render_pass1(result)
    render_pass2(result)

    sys.exit(0 if _result_ok(result) else 1)


if __name__ == '__main__':
    main()
