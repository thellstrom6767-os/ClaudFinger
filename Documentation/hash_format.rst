Hash Format for Cryptographic Ledger Attestation
==================================================

This document specifies the canonical text format used to compute the SHA-256 hash
of each entry in the ledger's hash chain. It is intentionally self-contained: no
accounting software, no libraries, and no knowledge of SIE file format is required
to reconstruct a hash from ledger data.

See :doc:`ledger_attestation` for an explanation of what the hash chain proves and
how it is used.


General Rules
-------------

- Encoding: **UTF-8**.
- Line endings: **LF** (``\n``), not CRLF.
- The hash of an entry is ``sha256(canonical_text)`` where ``canonical_text`` is
  the complete text document described below — no further concatenation or wrapping.
- Amounts: always **2 decimal places**, explicit negative sign for negative values,
  no thousands separator. Examples: ``10000.00``, ``-5000.00``, ``0.00``.
- Account numbers: as they appear in the ledger (numeric string, no padding).
- Dates: **ISO 8601**, ``YYYY-MM-DD``.
- Hashes embedded in the text (``PREV``, ``UNDERLAG``): lowercase hex, 64 characters.


Opening Balance Record (IB)
----------------------------

The chain root is computed from the ``#IB`` records in the SIE file. Its canonical
text has the following structure::

    PREV
    0000000000000000000000000000000000000000000000000000000000000000

    IB
    {fiscal_year}
    {account}:{amount}
    {account}:{amount}
    ...

Rules:

- ``PREV`` is always the 64-character all-zeros string. The IB record is always
  the genesis of the chain.
- ``{fiscal_year}`` is the four-digit year as an integer (e.g. ``2024``).
- One ``{account}:{amount}`` line per IB record, **sorted ascending by account number**.
- Accounts with a zero opening balance are omitted.

Example::

    PREV
    0000000000000000000000000000000000000000000000000000000000000000

    IB
    2024
    1010:10000.00
    1510:25000.00
    2081:-50000.00
    2099:-5000.00


Voucher Record (A-series)
--------------------------

Each A-series voucher has a canonical text with the following structure::

    PREV
    {previous_hash}

    UNDERLAG
    {underlag_hash}
    {underlag_hash}
    ...

    VOUCHER
    {series}:{number}
    {date}
    {reg_date}
    {description}
    {account}:{amount}
    {account}:{amount}
    ...

Rules:

- ``{previous_hash}`` is the ``voucher_hash`` of the preceding entry: the IB root
  hash for the first A voucher, or the previous A voucher's hash for all others.
- ``UNDERLAG`` section lists the SHA-256 hash of each attached document (underlag
  file), **sorted ascending by filename**. If there are no attachments the entire
  ``UNDERLAG`` section (header line and all hash lines) is omitted.
- ``{series}:{number}`` — e.g. ``A:47``.
- ``{date}`` is the economic date of the voucher (``YYYY-MM-DD``).
- ``{reg_date}`` is the registration date — when the entry was recorded in the
  system (``YYYY-MM-DD``). Including it ensures that altering the registration
  date invalidates the hash.
- ``{description}`` is the voucher text field, reproduced exactly (no trimming).
- One ``{account}:{amount}`` line per transaction, **sorted ascending by account
  number**.
- Blank lines between sections are part of the format and must be present exactly
  as shown.

Example without attachments::

    PREV
    a3f8c2d1e4b7f9206c5d8e1a4b7c0f3d6e9a2b5c8d1e4f7a0b3c6d9e2f5a8b1

    VOUCHER
    A:47
    2024-03-15
    2024-03-15
    Konsultarvode mars
    1510:15000.00
    3011:-15000.00

Example with attachments::

    PREV
    a3f8c2d1e4b7f9206c5d8e1a4b7c0f3d6e9a2b5c8d1e4f7a0b3c6d9e2f5a8b1

    UNDERLAG
    4b9e1f2a3c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d0e1
    7c3d8e5b6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3

    VOUCHER
    A:48
    2024-03-20
    2024-03-21
    Kontorsmaterial
    2640:200.00
    4010:-1000.00
    2610:800.00


Computing Underlag Hashes
--------------------------

Each underlag hash is the SHA-256 of the raw file bytes (not the filename, not any
metadata). Files are hashed independently. The order in the ``UNDERLAG`` section is
determined by sorting the filenames ascending — not by hash value.

Example using standard tools::

    sha256sum Verifikation_A47_1av2.pdf
    sha256sum Verifikation_A47_2av2.pdf


Verification Procedure
-----------------------

To verify a single voucher hash from raw data:

1. Collect the field values from the ledger: series, number, date, description,
   transactions.
2. Compute SHA-256 of each attachment file.
3. Retrieve the previous entry's ``voucher_hash`` from the chain table (or use
   all-zeros for the IB root).
4. Assemble the canonical text exactly as specified above.
5. Compute ``sha256(canonical_text)`` and compare to the stored ``voucher_hash``.

No accounting software is required. Standard tools (Python ``hashlib``, ``sha256sum``,
``openssl dgst -sha256``) are sufficient.
