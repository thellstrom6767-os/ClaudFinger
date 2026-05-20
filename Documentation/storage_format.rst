Internal Storage Format
=======================

.. contents:: Table of Contents
   :depth: 3
   :local:

Overview
--------

A ledger year in bokforing consists of up to four files that always share
the same path stem:

.. code-block:: text

   ledger_2024.se               ← primary ledger (SIE 4, plain text, CP437)
   ledger_2024_underlag/        ← binary supporting documents
   ledger_2024_underlag.db      ← SQLite index for the underlag
   ledger_2024.si5              ← optional SIE 5 archive (zip)

The ``.se`` file is the single source of truth for accounting data.
The underlag directory and database are optional companions; they exist
only when documents have been attached.  The ``.si5`` archive is a
derived artefact produced on demand by ``sie5export``; it can be
deleted at any time and regenerated.


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

Directory layout
~~~~~~~~~~~~~~~~

.. code-block:: text

   ledger_2024_underlag/
   ├── Verifikation_A1.pdf
   ├── Verifikation_A5[1av2].pdf
   ├── Verifikation_A5[2av2].pdf
   └── Verifikation_A20.jpg

The directory name is always the ledger file stem followed by
``_underlag``.  Files are named using the **Verifikation** convention:

.. code-block:: text

   Verifikation_{series}{number}.{ext}           ← single file
   Verifikation_{series}{number}[1av{n}].{ext}   ← first of n files
   Verifikation_{series}{number}[{i}av{n}].{ext} ← i-th of n files

When a second file is added to a voucher that previously had only one,
the existing file is renamed automatically from the single-file form to
``[1av2]``, and the new file is placed as ``[2av2]``.  The SQLite
database is updated in the same transaction.

SQLite schema
~~~~~~~~~~~~~

Database file: ``ledger_2024_underlag.db``

.. code-block:: sql

   CREATE TABLE underlag (
       id            INTEGER PRIMARY KEY AUTOINCREMENT,
       series        TEXT    NOT NULL,
       number        INTEGER NOT NULL,
       filename      TEXT    NOT NULL,   -- stored name in the directory
       original_name TEXT    NOT NULL,   -- name of the file as supplied
       added_at      TEXT    NOT NULL    -- ISO date (YYYY-MM-DD)
   );

The ``filename`` column is updated whenever the file is renamed due to
a change in the total count for a voucher.  The ``original_name``
column is immutable and preserves the name of the source file at the
time it was added.

The database is the authoritative index.  If a file exists on disk but
has no row in ``underlag`` it is not considered part of the store.  If
a row exists but the file has been deleted outside the application,
``underlag open`` will silently skip it.

Relationship to the SIE 4 file
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The underlag store is a *companion* to the ledger; the SIE 4 file
contains no reference to supporting documents.  The link between a
voucher (series + number) and its documents exists only in the SQLite
database.  When a SIE 5 package is exported these links are used to
embed ``DocumentReference`` elements in the XML.


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

The ``.se`` file is the only file that needs to be backed up to
preserve accounting data.  It is plain text and compresses well.  The
underlag directory and SQLite database should be backed up alongside it
if the original source documents are not retained elsewhere.

A SIE 5 export (``sie5export``) produces a single archive that
contains both the ledger data and all attached documents; it is a
convenient single-file backup artefact.
