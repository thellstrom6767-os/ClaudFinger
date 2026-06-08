"""RFC 3161 trusted timestamping.

Builds a TimeStampRequest in pure Python (no ASN.1 library needed), POSTs it
to a TSA, verifies the response cryptographic signature via openssl, and
extracts the UTC timestamp from the TSR.
"""
from __future__ import annotations

import os
import re
import secrets
import subprocess
import tempfile
from datetime import datetime, timezone

import requests as _requests

_DIGICERT_TSA_URL = 'http://timestamp.digicert.com'

# SHA-256 AlgorithmIdentifier DER: SEQUENCE { OID 2.16.840.1.101.3.4.2.1, NULL }
_SHA256_ALG_ID = bytes.fromhex('300d06096086480165030402010500')


# ── Minimal DER helpers ───────────────────────────────────────────────────────

def _der_tlv(tag: int, content: bytes) -> bytes:
    n = len(content)
    if n < 0x80:
        length = bytes([n])
    elif n < 0x100:
        length = bytes([0x81, n])
    else:
        length = bytes([0x82, n >> 8, n & 0xff])
    return bytes([tag]) + length + content


def _der_seq(content: bytes) -> bytes:
    return _der_tlv(0x30, content)


def _der_int(value: int) -> bytes:
    b = value.to_bytes((value.bit_length() + 8) // 8, 'big').lstrip(b'\x00') or b'\x00'
    if b[0] & 0x80:
        b = b'\x00' + b
    return _der_tlv(0x02, b)


def _der_skip_len(data: bytes, pos: int) -> tuple[int, int]:
    """Return (content_length, bytes_consumed_for_length_field)."""
    b = data[pos]
    if b < 0x80:
        return b, 1
    n = b & 0x7f
    return int.from_bytes(data[pos + 1: pos + 1 + n], 'big'), 1 + n


# ── Request builder ───────────────────────────────────────────────────────────

def _build_ts_request(hash_hex: str) -> bytes:
    """Return a DER-encoded RFC 3161 TimeStampRequest for the given SHA-256 hash."""
    hash_bytes = bytes.fromhex(hash_hex)
    version     = _der_int(1)
    msg_imprint = _der_seq(_SHA256_ALG_ID + _der_tlv(0x04, hash_bytes))
    nonce       = _der_int(secrets.randbits(64))
    cert_req    = bytes.fromhex('0101ff')   # BOOLEAN TRUE
    return _der_seq(version + msg_imprint + nonce + cert_req)


# ── Response parsing ──────────────────────────────────────────────────────────

def _check_tsr_status(tsr_bytes: bytes) -> None:
    """Parse PKIStatusInfo.status; raise RuntimeError if not Granted (0)."""
    try:
        pos = 0
        assert tsr_bytes[pos] == 0x30           # outer SEQUENCE tag
        pos += 1
        _, lb = _der_skip_len(tsr_bytes, pos)
        pos += lb                               # skip outer length → at PKIStatusInfo
        assert tsr_bytes[pos] == 0x30           # PKIStatusInfo SEQUENCE tag
        pos += 1
        _, lb = _der_skip_len(tsr_bytes, pos)
        pos += lb                               # skip PKIStatusInfo length → at status
        assert tsr_bytes[pos] == 0x02           # INTEGER tag
        pos += 1
        int_len, lb = _der_skip_len(tsr_bytes, pos)
        pos += lb
        status = int.from_bytes(tsr_bytes[pos: pos + int_len], 'big')
    except (AssertionError, IndexError) as exc:
        raise RuntimeError(f'Could not parse TSA response status: {exc}')
    if status != 0:
        raise RuntimeError(f'TSA returned status {status} (0=Granted expected)')


def _extract_gentime(tsr_bytes: bytes) -> str:
    """Return the TSR's genTime as an ISO 8601 UTC string via openssl."""
    with tempfile.NamedTemporaryFile(suffix='.tsr', delete=False) as f:
        f.write(tsr_bytes)
        tsr_path = f.name
    try:
        result = subprocess.run(
            ['openssl', 'ts', '-reply', '-in', tsr_path, '-text'],
            capture_output=True, text=True,
        )
        m = re.search(r'Time stamp:\s+(.+)', result.stdout)
        if not m:
            raise RuntimeError(
                f'Could not find timestamp in TSA response text:\n{result.stdout[:400]}'
            )
        ts_str = re.sub(r'\s+GMT\s*$', '', m.group(1).strip())
        for fmt in ('%b %d %H:%M:%S.%f %Y', '%b %d %H:%M:%S %Y'):
            try:
                return datetime.strptime(ts_str, fmt).replace(
                    tzinfo=timezone.utc
                ).isoformat()
            except ValueError:
                continue
        raise RuntimeError(f'Could not parse timestamp string: {ts_str!r}')
    finally:
        os.unlink(tsr_path)


def _verify_tsr(tsr_bytes: bytes, req_der: bytes, ca_cert_path: str) -> None:
    """Verify TSR signature against ca_cert_path using openssl ts -verify."""
    with tempfile.NamedTemporaryFile(suffix='.tsr', delete=False) as f:
        f.write(tsr_bytes)
        tsr_path = f.name
    with tempfile.NamedTemporaryFile(suffix='.tsq', delete=False) as f:
        f.write(req_der)
        req_path = f.name
    try:
        result = subprocess.run(
            ['openssl', 'ts', '-verify',
             '-queryfile', req_path,
             '-in', tsr_path,
             '-CAfile', ca_cert_path],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f'TSA response verification failed:\n{result.stderr.strip()}'
            )
    finally:
        os.unlink(tsr_path)
        os.unlink(req_path)


def verify_tsr_by_digest(tsr_bytes: bytes, hash_hex: str, ca_cert_path: str) -> None:
    """Verify a stored TSR against a known hash using openssl ts -verify -digest.

    Does not require the original TimeStampRequest (no nonce check).
    Raises RuntimeError if the signature is invalid or the digest does not match.
    """
    with tempfile.NamedTemporaryFile(suffix='.tsr', delete=False) as f:
        f.write(tsr_bytes)
        tsr_path = f.name
    try:
        result = subprocess.run(
            ['openssl', 'ts', '-verify',
             '-digest', hash_hex,
             '-in', tsr_path,
             '-CAfile', ca_cert_path],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f'TSA response verification failed:\n{result.stderr.strip()}'
            )
    finally:
        os.unlink(tsr_path)


# ── Public API ────────────────────────────────────────────────────────────────

def request_timestamp(
    hash_hex: str,
    tsa_url: str = _DIGICERT_TSA_URL,
    ca_cert_path: str | None = None,
) -> tuple[bytes, str]:
    """Request an RFC 3161 timestamp for hash_hex.

    Posts a TimeStampRequest to tsa_url, checks the PKIStatus, verifies the
    cryptographic signature (using certifi's CA bundle by default), and
    extracts the UTC timestamp.

    Returns (tsr_bytes, iso_timestamp_utc).
    Raises RuntimeError on network error, TSA refusal, or verification failure.
    """
    req_der = _build_ts_request(hash_hex)

    try:
        resp = _requests.post(
            tsa_url,
            data=req_der,
            headers={'Content-Type': 'application/timestamp-query'},
            timeout=30,
        )
        resp.raise_for_status()
    except Exception as exc:
        raise RuntimeError(f'TSA request to {tsa_url} failed: {exc}')

    tsr_bytes = resp.content
    _check_tsr_status(tsr_bytes)

    if ca_cert_path is None:
        import certifi
        ca_cert_path = certifi.where()

    _verify_tsr(tsr_bytes, req_der, ca_cert_path)
    return tsr_bytes, _extract_gentime(tsr_bytes)
