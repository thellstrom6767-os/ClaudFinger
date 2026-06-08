Internal Storage Format
=======================

.. contents:: Table of Contents
   :depth: 3
   :local:

Overview
--------

A ledger year in bokforing consists of up to three files that always share
the same path stem:

.. code-block:: text

   ledger_2024_ledger.db        ← authoritative store (SQLite)
   ledger_2024.se               ← export-only SIE 4 file (written by export/sie5export)
   ledger_2024.si5              ← optional SIE 5 archive (zip)

``ledger_2024_ledger.db`` is the single source of truth.  It contains all
accounting data (metadata, chart of accounts, balance entries, vouchers,
transactions) **and** all supporting documents stored as BLOBs.  The ``.se``
file is a derived artefact written only by the ``export`` and ``sie5export``
commands; it is never read by the application after the initial migration.
The ``.si5`` archive is also a derived artefact produced on demand by
``sie5export``; it can be deleted at any time and regenerated.

**Migration from the old format**

When any command opens a ledger path that points to a ``.se`` file (or a
``_ledger.db`` that does not yet exist), and no ``_ledger.db`` is present, the
application auto-migrates:

1. Parses the ``.se`` file into a ``SIEFile`` object.
2. Creates the ``_ledger.db`` and writes all data (meta, accounts, balances,
   vouchers, transactions).
3. If a legacy ``_underlag.db`` / ``_underlag/`` pair exists, reads each file
   from the directory, stores it as a BLOB in the new DB, then deletes both
   the ``_underlag.db`` and ``_underlag/`` directory.
4. The original ``.se`` file is **not** deleted; it remains as a backup.

After migration, all subsequent commands use ``_ledger.db`` exclusively.


SQLite Ledger File (``_ledger.db``)
------------------------------------

Naming and path
~~~~~~~~~~~~~~~

The DB file is always named ``{stem}_ledger.db`` where ``{stem}`` is the
path stem of the associated ``.se`` file.  Examples:

.. code-block:: text

   ledger_2024.se               → ledger_2024_ledger.db
   Retsina_Consulting_AB_2023.se → Retsina_Consulting_AB_2023_ledger.db

Both path forms are accepted by all commands and the ``--ledger`` option.

Schema
~~~~~~

.. code-block:: sql

   CREATE TABLE meta (
       key   TEXT PRIMARY KEY,
       value TEXT NOT NULL DEFAULT ''
   );

   CREATE TABLE accounts (
       number TEXT PRIMARY KEY,
       label  TEXT NOT NULL DEFAULT '',
       ktyp   TEXT,
       sru    TEXT NOT NULL DEFAULT '[]'  -- JSON array of SRU code strings
   );

   CREATE TABLE balances (
       type    TEXT NOT NULL,             -- 'IB', 'UB', or 'RES'
       account TEXT NOT NULL,
       amount  TEXT NOT NULL,             -- Decimal as plain string
       PRIMARY KEY (type, account)
   );

   CREATE TABLE vouchers (
       id        INTEGER PRIMARY KEY AUTOINCREMENT,
       series    TEXT    NOT NULL,
       number    INTEGER NOT NULL,
       date      TEXT    NOT NULL DEFAULT '',
       label     TEXT    NOT NULL DEFAULT '',
       reg_date  TEXT    NOT NULL DEFAULT '',
       signature TEXT    NOT NULL DEFAULT '',
       UNIQUE(series, number)
   );

   CREATE TABLE transactions (
       id         INTEGER PRIMARY KEY AUTOINCREMENT,
       voucher_id INTEGER NOT NULL REFERENCES vouchers(id),
       seq        INTEGER NOT NULL,
       account    TEXT    NOT NULL,
       amount     TEXT    NOT NULL,       -- Decimal as plain string
       date       TEXT    NOT NULL DEFAULT '',
       label      TEXT    NOT NULL DEFAULT ''
   );

   CREATE TABLE underlag (
       id            INTEGER PRIMARY KEY AUTOINCREMENT,
       series        TEXT NOT NULL,
       number        INTEGER NOT NULL,
       original_name TEXT NOT NULL,
       added_at      TEXT NOT NULL,       -- ISO date (YYYY-MM-DD)
       data          BLOB NOT NULL,       -- zlib-compressed when compressed=1
       sha256        TEXT,               -- hex SHA-256 of original (decompressed) data; NULL only in legacy rows (backfilled on open)
       compressed    INTEGER NOT NULL DEFAULT 0   -- 1 = data is zlib-compressed
   );

   CREATE TABLE chain (
       voucher_series  TEXT    NOT NULL,   -- 'IB' for root, 'A' for vouchers
       voucher_number  INTEGER NOT NULL,   -- 0 for IB root
       voucher_hash    TEXT    NOT NULL,   -- SHA-256 hex of canonical text
       tsr_token       BLOB,              -- RFC 3161 response; NULL until lock
       tsa_timestamp   TEXT,              -- ISO timestamp; NULL until lock
       PRIMARY KEY (voucher_series, voucher_number)
   );

The ``chain`` table is created on first use (``_create_tables`` is called on
every open).  ``voucher_hash`` rows are written by ``hash``.  ``tsr_token``
and ``tsa_timestamp`` are written by ``lock`` for the single tail entry that
is timestamped.  The hash format is specified in :doc:`hash_format`.

The ``meta`` table stores the ``SIEFile`` header fields as key/value pairs
(``program``, ``program_version``, ``gen_date``, ``gen_author``, ``org_nr``,
``company_name``, ``contact``, ``street``, ``zip_city``, ``phone``,
``year_begins``, ``year_ends``, ``currency``).

The ``sru`` column in ``accounts`` is a JSON array of SRU code strings
(``["7281"]`` or ``[]`` for accounts with no SRU mapping).

Amounts in ``balances`` and ``transactions`` are stored as plain decimal
strings (``"19058.02"``) to preserve exact precision without floating-point
error.

Atomic writes
~~~~~~~~~~~~~

``store.save_ledger()`` wraps all DELETE + INSERT operations in a single
SQLite transaction (``BEGIN`` … ``COMMIT`` / ``ROLLBACK``).  A crash at any
point leaves the database either fully intact or fully updated — the ``underlag``
table is never touched by ``save_ledger``; its rows are managed separately by
the ``underlag`` module.

WAL mode
~~~~~~~~

All connections open with ``PRAGMA journal_mode=WAL``.  WAL gives safe
concurrent reads while a write is in progress, which matters if the
balance/list/verify commands are run while an add is underway in another
terminal.

Underlag — BLOB storage
~~~~~~~~~~~~~~~~~~~~~~~

Supporting documents are stored directly in the ``underlag`` table as raw
binary BLOBs.  There is no ``filename`` column; the **Verifikation naming
convention** is derived on-the-fly from the insertion order within each
``(series, number)`` group:

.. code-block:: text

   single file  → Verifikation_{series}{number}.{ext}
   i-th of n    → Verifikation_{series}{number}[{i}av{n}].{ext}

This derived name is returned by ``list_for_voucher`` in the ``filename``
field and is used when exporting to ``_underlag/`` or embedding in SIE 5.

The ``export`` command and ``sie5export`` write the BLOBs to disk using
this convention.  The ``underlag open`` command extracts a BLOB to
``/tmp/bokforing_underlag/{id}_{original_name}`` and opens it with
``xdg-open``.

**Compression** — new BLOBs are stored zlib-compressed (``compressed=1``).
Existing rows added before this feature have ``compressed=0`` and are stored
as raw bytes.  All read paths (``get_data``, ``open_file``, ``export_underlag``,
``export_sie``) decompress transparently based on the flag.

**Integrity** — the ``sha256`` column stores the hex SHA-256 digest of the
*original, decompressed* data at insert time.  Existing DBs are backfilled
automatically on ``open_ledger``.  Both ``export_underlag`` and ``export_sie``
recompute the digest from the decompressed data after writing the file and
raise ``RuntimeError`` if there is a mismatch, catching in-database corruption
before it reaches exported files.


SIE 4 Ledger File (``.se``)
----------------------------

File format
~~~~~~~~~~~

SIE 4 is a Swedish industry-standard plain-text format defined by the
`SIE Group <http://www.sie.se>`_.  The file is encoded in **CP437**
(IBM PC code page 437, also called "PC8" or "DOS Latin US").  Every
line starts with a ``#`` tag followed by space-separated fields; quoted
strings may contain spaces.

The ``bokforing`` app writes files in SIE type 4 (``#SIETYP 4``), which
includes the full transaction log.

Section ordering
~~~~~~~~~~~~~~~~

A well-formed SIE 4 file contains the following sections in order:

1. **File metadata** — identity of the producing software, generation
   date and author, company details, fiscal period, currency.
2. **Chart of accounts** — account number, label, type, and SRU codes.
3. **Balance entries** — opening balances (IB), closing balances (UB),
   and period results (RES).
4. **Vouchers** — the transaction journal.

File metadata
~~~~~~~~~~~~~

.. code-block:: sie

   #FLAGGA 0
   #PROGRAM "Claude's converter" "2026-05-20"
   #FORMAT PC8
   #GEN 20260520 "thomas hellström"
   #SIETYP 4
   #ORGNR 556927-9168
   #ADRESS "C/O Thomas Hellström" "Jättestugorna 12" "42472 Olofstorp" "+46704976916"
   #FNAMN "Retsina Consulting AB"
   #RAR 0 20240101 20241231
   #VALUTA SEK

``#RAR 0`` declares the primary fiscal year.  The ``0`` is the year
index (0 = current year; earlier years would be −1, −2 etc., but
bokforing always writes a single-year file).

Chart of accounts
~~~~~~~~~~~~~~~~~

.. code-block:: sie

   #KONTO 1941 "Affärskonto Handelsbanken"
   #KTYP  1941 T
   #SRU   1941 7281

``#KONTO`` declares an account in the chart.  ``#KTYP`` assigns a type
code; ``#SRU`` links the account to a field on the Swedish corporate
income-tax return (INK2).

**Type codes (KTYP)**

===== =========== ===========================================
Code  Swedish     Meaning
===== =========== ===========================================
``T`` Tillgång    Asset (1xxx accounts)
``S`` Skuld       Liability (some 2xxx accounts)
``I`` Intäkt      Income (3xxx accounts)
``K`` Kostnad     Cost/expense (4xxx–8xxx accounts)
===== =========== ===========================================

2xxx accounts (equity and liabilities) carry no ``#KTYP`` entry in
the files produced by this application.

Balance entries
~~~~~~~~~~~~~~~

.. code-block:: sie

   #IB 0 1941  19058.02     ← opening balance: account 1941, year index 0
   #UB 0 1941  59849.73     ← closing balance
   #RES 0 3030 -34962.61    ← period result (income/expense accounts)

Only accounts with non-zero values appear in these sections.  The
``0`` is the year index, matching ``#RAR 0``.

**Sign convention** — all amounts use the accounting (double-entry)
sign convention:

* Asset accounts (1xxx): positive = debit = the asset has a value.
* Liability and equity accounts (2xxx): negative = credit = the
  liability exists.
* Income accounts (3xxx): negative = credit = income earned.
* Expense accounts (4xxx–8xxx): positive = debit = cost incurred.

This is the raw SIE convention.  The report commands *negate* P&L
amounts for display so that income appears positive and costs appear
negative in the printed reports.

Vouchers and transactions
~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: sie

   #VER "A" 13 20230726 "Årsstämma omfördelning" 20240729 "TH"
   {
   #TRANS 2099 {} -2785.76 20230726 ""         0.0
   #TRANS 2091 {}  2785.76 20230726 ""         0.0
   #TRANS 2091 {} 30000.00 20230726 "Utdelning" 0.0
   #TRANS 2893 {} -30000.00 20230726 "Utdelning" 0.0
   }

The ``#VER`` fields are:

.. code-block:: text

   #VER  "series"  number  voucher-date  "description"  registration-date  "signature"

The ``#TRANS`` fields are:

.. code-block:: text

   #TRANS  account  {object-list}  amount  transaction-date  "label"  quantity

The ``{}`` is an empty object dimension list (used in more advanced SIE
implementations for cost-centre coding; always empty here).  The
trailing ``0.0`` is a quantity field, always zero.

Every correctly formed voucher satisfies:

.. math::

   \sum_{\text{transactions}} \text{amount} = 0

Append-only design
~~~~~~~~~~~~~~~~~~

During a fiscal year vouchers are never edited or deleted; they are
only **appended** to the end of the file.  The ``add`` command uses an
atomic write pattern:

1. Read the existing file into memory.
2. Prepare the new ``#VER`` block as text.
3. Write both to a temporary file (``ledger.se.tmp``).
4. Rename the temporary file over the original (``os.replace``).

``os.replace`` is atomic on POSIX systems, so a crash at any point
leaves the ledger either fully intact or fully updated — never
corrupted.

The header (``#FLAGGA`` … ``#VALUTA``), chart of accounts (``#KONTO``
… ``#SRU``), and balance entries (``#IB`` / ``#UB`` / ``#RES``) are
written once by ``init`` and never modified by the app during normal
operation.  They may be rewritten if the ledger is restored from a
SIE 5 archive.


Underlag Store
--------------

Documents are stored as BLOBs in the ``underlag`` table of ``_ledger.db``
(see schema above).  There is no separate ``_underlag/`` directory during
normal operation; files are extracted to disk only on explicit request.

Verifikation naming convention
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The same naming convention applies whether files are on disk (after
export) or displayed in ``underlag list``:

.. code-block:: text

   Verifikation_{series}{number}.{ext}           ← single file
   Verifikation_{series}{number}[1av{n}].{ext}   ← first of n files
   Verifikation_{series}{number}[{i}av{n}].{ext} ← i-th of n files

The name is derived from the insertion order of rows with the same
``(series, number)``.  The ``filename`` key returned by
``list_for_voucher`` reflects this computed name.

Export
~~~~~~

The ``export`` command writes the current state of the ledger to a ``.se``
file and, if any underlag is present, an ``_underlag/`` directory alongside
it using the Verifikation naming convention:

.. code-block:: text

   ledger_2024.se               ← generated by 'export'
   ledger_2024_underlag/        ← written alongside the .se file
   ├── Verifikation_A1.pdf
   ├── Verifikation_A5[1av2].pdf
   └── Verifikation_A5[2av2].pdf

This directory is not kept in sync with the DB automatically; it is a
point-in-time snapshot written by ``export`` or ``sie5export``.

Relationship to the SIE 4 file
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The ``.se`` file is an export artefact and contains no reference to
supporting documents.  The link between a voucher and its documents
lives entirely in the ``underlag`` table of ``_ledger.db``.  When a
SIE 5 package is exported, these links are used to embed
``DocumentReference`` elements in the XML.


SIE 5 Package (``.si5``)
--------------------------

The ``.si5`` file is a standard **zip archive** (``ZIP_DEFLATED``)
whose contents follow the SIE 5 specification
(``http://www.sie.se/sie5``).

Archive layout
~~~~~~~~~~~~~~

.. code-block:: text

   Retsina_Consulting_AB_2024-01-01_2024-12-31.si5
   ├── sie5.xml
   └── documents/
       ├── Verifikation_A1.pdf
       └── Verifikation_A5[1av2].pdf

``sie5.xml`` structure
~~~~~~~~~~~~~~~~~~~~~~

The XML document uses the default namespace ``http://www.sie.se/sie5``
and is formatted with two-space indentation.  The root element is
``<SIEDocument>``.

.. code-block:: xml

   <?xml version='1.0' encoding='utf-8'?>
   <SIEDocument xmlns="http://www.sie.se/sie5">

     <FileInfo>
       <SoftwareProduct Name="Claude's converter" Version="2026-05-20" />
       <FileCreation Time="2026-05-20T09:51:00" By="thomas hellström" />
       <Company OrganizationId="5569279168" Name="Retsina Consulting AB">
         <Address Street="Jättestugorna 12" PostalCode="42472"
                  City="Olofstorp" Country="SE" />
       </Company>
     </FileInfo>

     <FiscalYears>
       <FiscalYear Primary="true" Start="2024-01-01" End="2024-12-31"
                   AccountingCurrency="SEK" />
     </FiscalYears>

     <AccountingPlan>
       <Account Id="1385" Name="Värde kapitalförsäkring, Avanza" Type="Asset">
         <OpeningBalance amount="115000.00" />
         <ClosingBalance amount="115000.00" />
       </Account>
       <!-- ... -->
     </AccountingPlan>

     <Journals>
       <Journal Id="A" Name="">
         <JournalEntry Id="13" JournalDate="2023-07-26"
                       Text="Årsstämma omfördelning"
                       OriginalEntryDate="2024-07-29" CreatedBy="TH">
           <LedgerEntry AccountId="2099" Amount="-2785.76" />
           <LedgerEntry AccountId="2091" Amount="2785.76" />
           <LedgerEntry AccountId="2091" Amount="30000.00" Text="Utdelning" />
           <LedgerEntry AccountId="2893" Amount="-30000.00" Text="Utdelning" />
           <DocumentReference DocumentId="1" />
         </JournalEntry>
       </Journal>
     </Journals>

     <Documents>
       <Document Id="1" Name="Verifikation_A1.pdf"
                 ContentType="application/pdf" />
     </Documents>

   </SIEDocument>

**Account type mapping**

========= ====== ================================
SIE 5     SIE 4  Applied to
========= ====== ================================
Asset     T      1xxx accounts
Income    I      3xxx accounts
Cost      K      4xxx–8xxx accounts
Liability S      2100–2999 accounts
Equity    (none) 2000–2099 accounts
========= ====== ================================

**Round-trip limitations**

SIE 5 does not carry ``#SRU`` codes.  A ledger exported to SIE 5 and
then re-imported with ``sie5import`` will be functionally identical
in accounting terms but will lack the tax-return field mappings.  If
SRU codes are important, retain the original ``.se`` file or regenerate
them from a reference chart.


Sorting and Renumbering
-----------------------

The ``sort`` command is the only operation that rewrites the ledger file
in full.  It is intended as a one-time sanitise step for ledgers that
were built retrospectively with backdated vouchers in non-chronological
order.

**Sort key options**

``registration-date``
  Orders vouchers by when they were entered into the system.  Useful
  when bookkeeping was done after the fact: the registration dates
  reflect the actual entry sequence even if the voucher dates are
  spread across the fiscal year.

``voucher-date``
  Orders vouchers by the economic date of the transaction.

**Renumbering**

After sorting, vouchers within each series are renumbered 1, 2, 3, …
The underlying transaction amounts, dates, labels, and signatures are
not altered.

**Underlag renaming**

Because underlag is stored as BLOBs in ``_ledger.db``, renumbering
requires only a single SQL ``UPDATE`` on the ``underlag`` table.  No
file renames or two-pass strategies are needed.  The
``renumber_vouchers`` function in ``underlag.py`` issues one
``UPDATE underlag SET series=?, number=? WHERE series=? AND number=?``
per mapping entry and commits.

**Integrity**

``sort`` calls ``sie.write()`` which rewrites all sections (header,
accounts, balances, vouchers).  Run ``verify`` after sorting to confirm
all vouchers still balance.


Changing the Accounting Year
-----------------------------

Normal year transition
~~~~~~~~~~~~~~~~~~~~~~

At the end of a fiscal year the standard procedure is:

1. Ensure all year-end vouchers have been posted (tax provision,
   periodisation fund allocation, year-end result booking).
2. Run ``verify`` to confirm every voucher balances.
3. Optionally run ``report`` and ``balansrapport`` to produce the
   annual financial statements.
4. Run ``init`` to create the new year's ledger, which carries the
   closing balances forward as opening balances.

.. code-block:: bash

   python main.py verify
   python main.py report --prev-sie ledger_2023.se
   python main.py balansrapport
   python main.py init 2025 --from-sie ledger_2024.se

What carries forward
~~~~~~~~~~~~~~~~~~~~

Only **balance-sheet accounts** (account numbers 1000–2999) carry
forward.  Income and expense accounts (3000–8999) are not included in
the new year's ``#IB`` section; they implicitly start at zero.

Specifically, the following are *not* carried forward:

* ``#RES`` entries (period results — these belong to the closed year).
* ``#KTYP`` and ``#SRU`` entries — these are copied from the source
  file's chart of accounts, not from the balance section.
* Any income or expense account balance, even if the year-end closing
  entry has not yet been posted.

The new ledger inherits the full **chart of accounts** from the source
year so that the same account numbers and labels are available
immediately.

Closing entries and ``#UB`` status
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The app does not write ``#UB`` or ``#RES`` entries automatically; those
sections are only present in files that were generated by
``box_to_sie.py`` (which reads them from the BL Ekonomi export) or
restored via ``sie5import`` (which reads them from the SIE 5 XML).

For a ledger maintained entirely by bokforing:

* The running balance for every account is always computable as
  ``IB + Σ(transactions)``.
* There are no ``#UB`` entries; the closing balance lives implicitly in
  the transactions.
* When ``init`` is run for the next year, closing balances are computed
  on the fly from ``IB + transactions`` (the *open year* path).

This means:

.. code-block:: text

   Closed-year source (has #UB)  →  init uses #UB directly
   Open-year source (no #UB)     →  init computes IB + transactions

Both paths produce an identical result as long as all year-end vouchers
have been posted.

Mid-year corrections
~~~~~~~~~~~~~~~~~~~~

Because the SIE 4 file is append-only, correcting a posted voucher is
done by posting a **correction voucher** — a new ``#VER`` that exactly
reverses the erroneous amounts and then re-posts the correct amounts.
This is the standard double-entry method and preserves a full audit
trail.

There is no facility to edit or delete a voucher in place.  The raw
``.se`` file is a text file and can technically be edited with any text
editor, but doing so outside the application breaks the append-only
contract and may introduce encoding or formatting errors.

The one intentional exception to the append-only rule is the ``sort``
command, which rewrites the entire file in order to renumber vouchers.
See *Sorting and renumbering* below.

Changing the fiscal year period
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The fiscal year period is fixed at ``YYYY-01-01`` – ``YYYY-12-31``
when a ledger is created by ``init``.  If a non-calendar fiscal year
is required (e.g. July–June), the ``.se`` file's ``#RAR`` line and the
``year_begins`` / ``year_ends`` metadata must be set manually or via a
custom call to ``bokforing.sie.write()``.  This is not currently
exposed through a CLI option.


Data Integrity
--------------

Balancing invariant
~~~~~~~~~~~~~~~~~~~

Every individual voucher must satisfy:

.. math::

   \sum_{\text{transactions in voucher}} \text{amount} = 0

The ``verify`` command checks this for every voucher.

The overall balance sheet must satisfy:

.. math::

   \sum_{\text{asset accounts}} \text{balance}
   + \sum_{\text{liability/equity accounts}} \text{balance} = 0

This is verified by the ``balance`` command (the ``Balanced ✓`` footer)
and by ``init`` (the opening-balance summary).

Encoding
~~~~~~~~

The ``.se`` file uses **CP437** encoding.  Swedish characters (å, ä, ö,
Å, Ä, Ö) are valid in CP437 and are written correctly.  Characters
outside CP437 are replaced with ``?`` via Python's ``errors='replace'``
fallback; this should not arise with normal Swedish company names and
account labels.

The underlag SQLite database and the ``sie5.xml`` inside the package
use **UTF-8**.  No conversion is needed for document filenames.

Backup recommendations
~~~~~~~~~~~~~~~~~~~~~~

The ``_ledger.db`` file is the only file that needs to be backed up to
preserve all accounting data and supporting documents.  It is a standard
SQLite binary and compresses well (WAL mode writes are already sequential).

A SIE 5 export (``sie5export``) produces a single ``.si5`` archive that
contains the ledger data (as XML) and all embedded documents; it is a
convenient single-file backup artefact and can be used to restore a ledger
with ``sie5import``.

The ``export`` command writes a ``ledger_YYYY.se`` + ``_underlag/``
snapshot suitable for exchange with SIE 4-only tools or long-term archiving
alongside the DB.
