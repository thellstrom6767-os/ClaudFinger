Command Reference
=================

.. contents:: Table of Contents
   :depth: 3
   :local:

Overview
--------

**bokforing** is a command-line accounting tool backed by SIE 4 files.
Every command operates on a *ledger file* — a ``.se`` file in the
current directory — plus an optional companion underlag store that holds
binary supporting documents.

Invocation
~~~~~~~~~~

.. code-block:: bash

   python main.py [--ledger FILE] COMMAND [OPTIONS] [ARGS]

The global ``--ledger`` / ``-l`` option selects which ``.se`` file to
operate on.  If omitted the program looks for a single ``.se`` file in
the current directory and uses it automatically.  When more than one is
present you must supply ``--ledger`` explicitly.

.. code-block:: bash

   python main.py -l path/to/ledger_2024.se balance
   python main.py balance                       # auto-detects if only one .se


Global options
~~~~~~~~~~~~~~

.. option:: -l FILE, --ledger FILE

   Path to the SIE 4 ledger file.  Auto-detected when the current
   directory contains exactly one ``*.se`` file.


----

Ledger Initialisation
---------------------

init
~~~~

Create a new ledger year by carrying forward the closing balances of a
previous year.

.. code-block:: text

   Usage: main.py init [OPTIONS] YEAR

.. option:: YEAR

   The four-digit fiscal year to create (e.g. ``2024``).  The fiscal
   period is assumed to be the full calendar year
   (``YYYY-01-01`` – ``YYYY-12-31``).

.. option:: -f FILE, --from-sie FILE  *(required)*

   The SIE file for the previous year whose closing balances become
   the opening balances of the new year.

.. option:: -o FILE, --output FILE

   Path for the new ledger file.  Defaults to ``ledger_YYYY.se`` in
   the current directory.

**Behaviour**

The command reads the source file and determines closing balances using
one of two strategies, depending on the source:

* **Closed year** — the source contains explicit ``#UB`` entries
  (e.g. a file produced by ``box_to_sie.py`` or a prior ``sie5import``).
  These are used directly.
* **Open year** — the source has ``#IB`` entries and ``#VER`` vouchers but
  no ``#UB``.  Closing balances are computed as
  ``IB + Σ(all transactions)`` for every account.

In both cases only balance-sheet accounts (account numbers 1000–2999)
are carried forward.  Income and expense accounts (3000–8999) reset to
zero at the start of every year.

A balance check is printed.  If assets (1xxx) and
equity/liabilities (2xxx) do not cancel out the output is highlighted
in red.

**Example**

.. code-block:: bash

   # Initialise 2024 from the closed 2023 year
   python main.py init 2024 --from-sie ../retsinaconsultingab_2023.se

   # Chain years: 2025 from a running 2024 ledger
   python main.py init 2025 --from-sie ledger_2024.se

**Sample output**

.. code-block:: text

   Created ledger_2024.se
     Company : Retsina Consulting AB  (556927-9168)
     Period  : 2024-01-01 – 2024-12-31
     Source  : retsinaconsultingab_2023.se  (#UB entries (closed year))
     Accounts: 226  |  IB entries: 13

     Assets (1xxx)                       174,858.73
     Equity/liabilities (2xxx)          -174,858.73
     Balanced ✓


----

Daily Bookkeeping
-----------------

add
~~~

Add a new double-entry voucher interactively.

.. code-block:: text

   Usage: main.py add [OPTIONS]

The command prompts for a date, a description, and then one transaction
line at a time.  After each line the running balance is shown; when the
running balance reaches zero the voucher is balanced and ready to save.

Accounts can be entered as an exact four-digit number or as a
case-insensitive substring of the account label.

.. code-block:: text

   Date (YYYYMMDD) [20260520]:
   Description: Mobilräkning Tre
   Transactions — enter account number or name, empty line when done:
     Account: 6210
            → 6210  Telekommunikation
     Amount: 985.00
     Label:
     Running balance: +985.00
     Account: 2640
            → 2640  Ingående moms
     Amount: -246.25
     Label:
     Running balance: +738.75
     Account: 1941
            → 1941  Affärskonto Handelsbanken
     Amount: -738.75
     Label:
     Running balance: +0.00 ✓
     Account:
   ──────────────────────────────────────────────────────
     20260520  Mobilräkning Tre
     6210       985.00
     2640      -246.25
     1941      -738.75
   ──────────────────────────────────────────────────────
   Save? [Y/n]:
   Saved as A:31 in ledger_2024.se

The voucher is appended to the SIE file atomically (written to a
temporary file and then renamed) so a crash mid-write cannot corrupt
the existing data.

An unbalanced voucher (transactions not summing to zero) will trigger a
warning and a confirmation prompt before saving.  Unbalanced vouchers
are permitted but will fail the ``verify`` check.

sort
~~~~

Sort and renumber vouchers within each series, then rename any
attached underlag files to match the new numbers.

.. code-block:: text

   Usage: main.py sort [OPTIONS]

.. option:: --by [registration-date|voucher-date]

   The date field to sort by.  Default: ``registration-date``.

   ``registration-date``
     The date the entry was made in the accounting system.  Produces a
     ledger ordered by when transactions were recorded, which is useful
     when books were kept retrospectively and entries are backdated.

   ``voucher-date``
     The economic date of the transaction (invoice date, payment date,
     etc.).  Produces a ledger ordered by when events actually occurred.

.. option:: --dry-run

   Print the proposed renumbering table without writing any changes.
   Use this first to review what will move before committing.

**Behaviour**

Within each voucher series, vouchers are sorted by the chosen date
(stable sort — vouchers with the same date retain their relative
order).  They are then renumbered 1, 2, 3, … from the beginning of
the series.  Finally, any underlag files linked to moved vouchers are
renamed using a collision-safe two-pass strategy:

1. All affected files are first renamed to temporary names
   (``__sort_tmp_{id}.ext``) so that old and new numbers can freely
   overlap.
2. Files are renamed from the temporary names to their final
   ``Verifikation_A{n}[…]`` names, and the SQLite index is updated.

.. warning::

   ``sort`` rewrites the ledger file in full.  This is a one-time
   sanitise operation and intentionally breaks the normal append-only
   contract.  Run ``verify`` afterwards to confirm integrity.

**Example**

.. code-block:: bash

   # Preview
   python main.py sort --dry-run

   # Apply (prompts for confirmation)
   python main.py sort

   # Sort by transaction date instead
   python main.py sort --by voucher-date

   # Verify nothing broke
   python main.py verify

verify
~~~~~~

Check that every voucher in the ledger is balanced (all transaction
amounts sum to zero).

.. code-block:: text

   Usage: main.py verify [OPTIONS]

Exits with code 0 on success, code 1 if any unbalanced vouchers are
found.

.. code-block:: bash

   python main.py verify
   # All 30 vouchers in ledger_2024.se balance. ✓


----

Querying
--------

balance
~~~~~~~

Show current running balances for all accounts that are non-zero.

.. code-block:: text

   Usage: main.py balance [OPTIONS] [PREFIX]

.. option:: PREFIX

   Optional account-number prefix to filter results.  For example
   ``1`` shows only asset accounts, ``19`` shows only cash and bank.

The balance for each account is computed as ``IB + Σ(transactions)``.

At the foot of a full (unfiltered) report a totals section shows the
sum of assets (1xxx) and equity/liabilities (2xxx) and flags whether
the balance sheet balances.

**Examples**

.. code-block:: bash

   python main.py balance           # all non-zero accounts
   python main.py balance 1         # assets only
   python main.py balance 194       # account 1940/1941/… only

list
~~~~

List vouchers in the ledger, most recent first.

.. code-block:: text

   Usage: main.py list [OPTIONS]

.. option:: -n INTEGER

   Number of most recent vouchers to show.  Default: 20.

.. option:: --all

   Show every voucher regardless of count.

.. code-block:: bash

   python main.py list -n 5
   python main.py list --all

show
~~~~

Display full details of a specific voucher including all transaction
lines and their account labels.

.. code-block:: text

   Usage: main.py show [OPTIONS] REF

.. option:: REF

   Voucher reference.  Format: ``SERIES:NUMBER`` (e.g. ``A:13``) or
   just the number (e.g. ``13``, defaults to series ``A``).

.. code-block:: bash

   python main.py show A:13
   python main.py show 13

history
~~~~~~~

Show a chronological transaction history for a single account, with a
running balance column.

.. code-block:: text

   Usage: main.py history [OPTIONS] ACCOUNT

.. option:: ACCOUNT

   Account number (e.g. ``1941``) or a case-insensitive substring of
   the account label (e.g. ``handelsbanken``).

The opening balance (from ``#IB``) is shown as the first line if
non-zero.

.. code-block:: bash

   python main.py history 1941
   python main.py history handelsbanken


----

Reports
-------

Both report commands write a LibreOffice Calc (ODS) spreadsheet.
The output file is placed next to the ledger file by default.

report
~~~~~~

Generate a **Resultatrapport** (income statement / profit-and-loss
account).

.. code-block:: text

   Usage: main.py report [OPTIONS]

.. option:: -p FILE, --prev-sie FILE

   SIE file for the previous year.  When supplied a third column,
   *ACK FÖREG ÅR* (accumulated previous year), is populated and
   the *JMF%* (year-on-year comparison percentage) is computed.
   When omitted those columns show ``—``.

.. option:: -o FILE, --output FILE

   Output path.  Default: ``Resultatrapport_YYYY-MM-DD-YYYY-MM-DD.ods``
   in the same directory as the ledger.

**Report structure**

The report follows the standard Swedish BAS income statement layout:

.. code-block:: text

   RÖRELSEINTÄKTER
     Försäljning (3000–3799)
     Övriga rörelseintäkter (3800–3999)
   SUMMA RÖRELSEINTÄKTER

   RÖRELSEKOSTNADER
     Material och varor (4000–4999)
   Bruttovinst
     Övriga externa rörelseutgifter (5000–6999)
     Personalkostnader (7000–7699)
     Avskrivningar (7700–7999)
   SUMMA RÖRELSEKOSTNADER

   Rörelseresultat
   Finansiella poster (8300–8499)
   Resultat efter finansiella poster
   Extraordinära poster (8700–8799)
   Resultat efter extraordinära poster
   Bokslutsdispositioner (8800–8899)
   Resultat före skatt
   Skatter (8900–8998)
   ÅRETS RESULTAT

Sections containing no non-zero accounts in either year are omitted.

**Columns**

========== ======= ==========================================================
Column     Header  Content
========== ======= ==========================================================
B          DENNA   Net change (sum of transactions) for the current year
           PERIOD
C          OMS%    As a percentage of SUMMA RÖRELSEINTÄKTER
D          UTG     Year-to-date balance (identical to DENNA PERIOD for a
           SALDO   full-year report)
E          OMS%    Same percentage base
F          ACK     Previous year result from ``--prev-sie``
           FÖREG
           ÅR
G          JMF%    ``current / previous × 100``; shows ``###.#`` when the
                   value overflows ±999.9 % or the previous year was zero
========== ======= ==========================================================

**Sign convention**

Income accounts are shown as positive, cost accounts as negative —
the opposite of the SIE sign convention.  ``ÅRETS RESULTAT`` is
positive for a profitable year.

**Example**

.. code-block:: bash

   python main.py report --prev-sie ../retsinaconsultingab_2022.se

balansrapport
~~~~~~~~~~~~~

Generate a **Balansrapport** (balance sheet).

.. code-block:: text

   Usage: main.py balansrapport [OPTIONS]

.. option:: -o FILE, --output FILE

   Output path.  Default: ``Balansrapport_YYYY-MM-DD-YYYY-MM-DD.ods``
   in the same directory as the ledger.

**Report structure**

.. code-block:: text

   TILLGÅNGAR
     Anläggningstillgångar (1100–1399)
     Omsättningstillgångar
       Varulager (1400–1499)
       Fordringar (1500–1799)
       Kortfristiga placeringar (1800–1899)
       Kassa och bank (1900–1999)
     Summa omsättningstillgångar
   SUMMA TILLGÅNGAR

   EGET OCH FRÄMMANDE KAPITAL
     Eget kapital (2000–2099)
     Obeskattade reserver (2100–2199)
     Avsättningar (2200–2299)
     Långfristiga skulder (2300–2399)
     Kortfristiga skulder (2400–2999)
   SUMMA EGET OCH FRÄMMANDE KAPITAL

Sections with no non-zero accounts are omitted.

**Columns**

=========== ============= ==================================================
Column      Header        Content
=========== ============= ==================================================
B           ING BALANS    Opening balance (from ``#IB``) at start of year
C           DENNA PERIOD  Net change during the year (sum of transactions)
D           UTG SALDO     Closing balance = ING BALANS + DENNA PERIOD
=========== ============= ==================================================

**Sign convention**

SIE sign convention is preserved: asset accounts are positive, liability
and equity accounts are negative.  When the balance sheet is correct,
``SUMMA TILLGÅNGAR`` and ``SUMMA EGET OCH FRÄMMANDE KAPITAL`` will be
equal in magnitude and opposite in sign.

.. code-block:: bash

   python main.py balansrapport


----

Supporting Documents (Underlag)
--------------------------------

The ``underlag`` command group manages binary supporting documents
(receipts, invoices, bank statements) associated with individual
vouchers.  Files are stored in a directory and indexed in a SQLite
database, both placed next to the ledger file.

.. code-block:: text

   ledger_2024.se
   ledger_2024_underlag/          ← actual files
   ledger_2024_underlag.db        ← SQLite index

underlag add
~~~~~~~~~~~~

Attach one or more files to a voucher.

.. code-block:: text

   Usage: main.py underlag add [OPTIONS] REF FILES...

.. option:: REF

   Voucher reference (e.g. ``A:5`` or ``5``).

.. option:: FILES

   One or more file paths to attach.  Any file format is accepted;
   common types (PDF, JPEG, PNG, TIFF) receive appropriate MIME types
   in the SIE 5 manifest when exported.

Files are copied into the underlag directory and named following the
established **Verifikation** convention:

* Single file for a voucher → ``Verifikation_A5.pdf``
* Multiple files → ``Verifikation_A5[1av2].pdf``, ``Verifikation_A5[2av2].pdf``

When a second file is added to a voucher that previously had only one,
the existing file is automatically renamed to the ``[1avN]`` form.

.. code-block:: bash

   python main.py underlag add A:5 receipt.pdf
   python main.py underlag add A:20 page1.pdf page2.pdf
   python main.py underlag add 7 bank_statement.pdf   # series A assumed

underlag list
~~~~~~~~~~~~~

List stored underlag.

.. code-block:: text

   Usage: main.py underlag list [OPTIONS] [REF]

Without ``REF``: prints a summary table of all vouchers that have at
least one file, with file counts.

With ``REF``: shows the file-level detail for that voucher — database
ID, stored filename, original filename, and date added.

.. code-block:: bash

   python main.py underlag list          # summary: which vouchers have files
   python main.py underlag list A:5      # file detail for A:5

underlag open
~~~~~~~~~~~~~

Open all underlag files for a voucher with the system default viewer
(``xdg-open`` on Linux).

.. code-block:: text

   Usage: main.py underlag open [OPTIONS] REF

.. code-block:: bash

   python main.py underlag open A:5

underlag remove
~~~~~~~~~~~~~~~

Remove a stored underlag file by its database ID.

.. code-block:: text

   Usage: main.py underlag remove [OPTIONS] FILE_ID

.. option:: FILE_ID

   Integer database ID as shown by ``underlag list REF``.

When the last or only file for a voucher is removed the remaining files
(if any) are automatically renumbered to keep the ``[1avN]`` sequence
consistent.

.. code-block:: bash

   python main.py underlag list A:5     # find the ID
   python main.py underlag remove 3     # remove file with ID 3


----

SIE 5 Export / Import
----------------------

SIE 5 is an XML-based accounting exchange format packaged as a standard
zip file with the extension ``.si5``.  It combines ledger data with
embedded binary documents in a single self-contained archive.

sie5export
~~~~~~~~~~

Export the current ledger as a SIE 5 package, embedding any underlag
that has been attached to vouchers.

.. code-block:: text

   Usage: main.py sie5export [OPTIONS]

.. option:: -o FILE, --output FILE

   Output path.  Default: ``CompanyName_YYYY-MM-DD-YYYY-MM-DD.si5``
   placed next to the ledger file.

**Package contents**

.. code-block:: text

   CompanyName_2024-01-01_2024-12-31.si5  (zip)
   ├── sie5.xml
   └── documents/
       ├── Verifikation_A1.pdf
       ├── Verifikation_A5[1av2].pdf
       └── Verifikation_A5[2av2].pdf

The XML uses the ``http://www.sie.se/sie5`` namespace and contains:

* ``FileInfo`` — software, generation timestamp, company and address
* ``FiscalYears`` — fiscal period and currency
* ``AccountingPlan`` — all accounts with opening and closing balances
* ``Journals`` — all vouchers, each with transaction lines and
  ``DocumentReference`` elements for attached files
* ``Documents`` — manifest of attached files with content types

Vouchers without underlag have no ``DocumentReference`` elements.

.. code-block:: bash

   python main.py -l ledger_2024.se sie5export

sie5import
~~~~~~~~~~

Restore a ledger year from a SIE 5 package, recreating the SIE 4 file
and repopulating the underlag store.

.. code-block:: text

   Usage: main.py sie5import [OPTIONS] SI5_FILE

.. option:: SI5_FILE

   Path to the ``.si5`` file to restore from.

.. option:: -o FILE, --output FILE

   Output ``.se`` path.  Default: derived from the company name and
   fiscal year embedded in the package
   (e.g. ``Retsina_Consulting_AB_2023.se``), placed in the same
   directory as the ``.si5`` file.

**What is restored**

======================== =====
Data                     Restored
======================== =====
Company info, org nr     ✓
Accounts (number, label) ✓
Account type (ktyp)      ✓ (mapped from SIE 5 ``Type`` attribute)
Opening balances (#IB)   ✓
Closing balances (#UB)   ✓
Vouchers — all fields    ✓
Transactions             ✓
Underlag files           ✓ (extracted and re-registered in SQLite)
SRU codes                ✗ (not carried in SIE 5)
======================== =====

.. note::

   SRU codes are a SIE 4-specific construct used for tax-return
   mapping.  They are not part of the SIE 5 schema and will be absent
   from any file restored from a SIE 5 package.  If SRU codes are
   required, re-generate the ledger from the original source or add
   them manually.

**Example**

.. code-block:: bash

   python main.py sie5import ../Retsina_Consulting_AB_2023-01-01_2023-12-31.si5
   # → Retsina_Consulting_AB_2023.se
   # → Retsina_Consulting_AB_2023_underlag/
   # → Retsina_Consulting_AB_2023_underlag.db


----

Typical Workflows
-----------------

Starting a new fiscal year
~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   # 1. Close last year by creating this year's ledger
   python main.py init 2025 --from-sie ledger_2024.se

   # 2. Begin posting vouchers
   python main.py -l ledger_2025.se add

Daily voucher entry
~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   python main.py add
   python main.py underlag add A:31 receipt.pdf
   python main.py verify

End-of-year reporting
~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   python main.py report --prev-sie ../ledger_2023.se
   python main.py balansrapport
   python main.py verify

Archive and handover
~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   # Produce a single self-contained archive for the auditor
   python main.py sie5export
   # → CompanyName_2024-01-01_2024-12-31.si5

Restore from archive
~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   python main.py sie5import CompanyName_2024-01-01_2024-12-31.si5
   python main.py -l CompanyName_2024.se verify
   python main.py -l CompanyName_2024.se balance
