Cryptographic Ledger Attestation
=================================

This document describes how the accounting ledger proves three things independently
of the accounting software:

1. **Time** — when a set of vouchers was attested, certified by a trusted third party.
2. **Content** — exactly what each voucher and its attachments contained at that time.
3. **Order** — the relative ordering of all vouchers, including the opening balances.

These guarantees are provided by a SHA-256 hash chain anchored to the opening balances
and sealed with an RFC 3161 trusted timestamp from an external Time Stamping Authority (TSA).


Overview
--------

Vouchers pass through two states before becoming part of the permanent record:

- **P (Preliminary)** — working state. Can be sorted by date, edited, or discarded.
- **A (Attested)** — permanent record. Numbers and content are frozen.

Two commands control the transition:

- ``attest`` — promotes selected P vouchers to A and computes their chain hashes.
- ``lock``   — submits the chain tail hash to the TSA and stores the timestamp token.

These are typically run together at period end, but are separate steps to allow
offline work and batched timestamping.


The Hash Chain
--------------

Every entry in the permanent record — the opening balances and each A voucher — has
a SHA-256 hash that covers its own content, all attached documents, and the hash of
the previous entry. This creates a chain: changing any entry invalidates all
subsequent hashes.

The chain starts with the opening balances (``#IB`` records). Their hash is the
*chain root* and is computed once when the ledger year is initialised for attestation.
The first A voucher's hash depends on the chain root; every subsequent voucher depends
on the one before it.

For the exact text format used to compute each hash, see :doc:`hash_format`.


The Promotion Step (``attest``)
--------------------------------

``attest`` selects a subset of P vouchers and permanently promotes them to A::

    python main.py attest --before 2024-03-31
    python main.py attest P3 P7 P12

Selected vouchers are sorted chronologically and assigned the next available A numbers
(A<max+1>, A<max+2>, …). Existing A vouchers are never renumbered. Attachment files
are renamed from ``Verifikation_P{n}`` to ``Verifikation_A{n}`` to match.

For each promoted voucher the chain hash is computed and stored in the ledger database.
The TSA is **not** called at this stage; ``tsr_token`` is NULL until ``lock`` is run.


The Locking Step (``lock``)
----------------------------

``lock`` seals the current chain with a trusted timestamp::

    python main.py lock

It reads the hash of the last A voucher (the *chain tail*), submits it to the TSA,
and stores the signed timestamp token in the ledger database.

The TSA (DigiCert) returns an RFC 3161 ``TimeStampResponse`` token. This token:

- Is signed with DigiCert's private key.
- Embeds the exact hash that was submitted.
- Contains a certified time that cannot be backdated.
- Can be verified by anyone using DigiCert's public certificate, with no dependency
  on this software.

The token is stored as a binary blob in the ledger database alongside the chain tail
hash. A human-readable copy of the certified time is also stored for easy querying.

Because the chain tail covers every prior entry, a single TSA call proves the time
and content of the entire chain up to that point.


What Is Proved
--------------

After ``attest`` and ``lock`` have been run on a set of vouchers, the following can
be established by any party without access to the accounting software:

- The opening balances had a specific content (chain root hash).
- Each voucher had a specific content and a specific set of attachments
  (per-voucher hash, reconstructable from the canonical text format in :doc:`hash_format`).
- The vouchers were in a specific order relative to each other (the chain structure).
- The chain tail existed before the TSA-certified timestamp (the RFC 3161 token,
  verifiable with ``openssl ts -verify`` against DigiCert's public certificate).

Any modification to a voucher, its attachments, or the opening balances after
``lock`` is run will produce a hash mismatch that is immediately detectable.


TSA Certificate
---------------

The DigiCert TSA root certificate is stored in this repository at
``Documentation/tsa_certs/digicert_tsa_root.pem``. Keeping it in the repository
ensures that tokens can be verified in the future without depending on DigiCert's
website remaining available.

Before trusting the certificate, verify its SHA-256 fingerprint against DigiCert's
published value::

    openssl x509 -in Documentation/tsa_certs/digicert_tsa_root.pem \
        -noout -fingerprint -sha256

The certificate file was fetched from DigiCert's TSA documentation on <DATE>.
Update this date and re-verify the fingerprint if the certificate is ever replaced.


Verifying Integrity Manually
-----------------------------

To verify the chain without using the accounting software:

1. For each entry (IB root, then each A voucher in order), reconstruct the canonical
   text as specified in :doc:`hash_format`.
2. Compute ``sha256`` of that text. It must match the stored ``voucher_hash``.
3. Confirm the ``PREV`` field in each entry matches the ``voucher_hash`` of the
   preceding entry (all-zeros for the IB root).
4. Export the TSA token from the database and verify it::

       openssl ts -verify -in token.tsr -digest <chain-tail-hash> \
           -CAfile Documentation/tsa_certs/digicert_tsa_root.pem

   A successful verification confirms DigiCert signed the chain tail at the
   recorded time.
