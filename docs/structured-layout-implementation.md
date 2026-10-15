# Verified worksheet regions and hierarchical headers

## Scope

The parser can now preserve several logical tables in a worksheet with explicit
decorated section headings (`=== title ===` or `--- title ---`), including metric
key/value sections and ordinary distribution tables. Recognition is structural,
not keyed to a workbook filename or the name of a summary sheet.

The existing reader, input resource limits, strict value conversion, and atomic
materialization remain in use. No new dependency or LLM parsing stage was added.

## Data and evidence

- Logical tables retain their original worksheet name, absolute source rows,
  cells, hidden-row facts, formula facts, and merged-cell lineage. Region titles
  and ranges are included in the manifest, and titles appear in the catalog.
- Metric tables store `metric`, `value_raw`, `numeric_value`, and `unit`. A percent
  numeric value is a ratio. Other units are preserved without conversion.
  Unrecognized text, timestamps, and paths keep their original value; unsupported
  numeric normalization remains NULL rather than losing precision.
- Numeric/unit columns are explicitly derived from the original B cell, and are
  re-derived during materialization to reject inconsistent values. They do not
  invent source cells or replace the original source fact.
- Workbook coverage records require every original occupied source coordinate to
  belong to exactly one recognized region. Missing or overlapping regions fail
  validation and materialization.
- Nonbusiness row classification is shared with the old parser. Excluded notes,
  repeated headers, and totals are retained as evidence; column inference uses
  included records, not excluded header text.

## Headers and formulas

Two/three-level and bounded deeper merged header trees are flattened into complete
field paths, with original header coordinates in `header_cells`. Header recovery
requires textual labels and horizontal grouping evidence. A vertical merge alone
cannot declare a numeric or textual first business record to be a header.

An isolated decorated heading stored as an Excel formula is interpreted as a
literal heading only inside a fully recognized layout. The original formula-typed
fact is retained. Actual numerical formulas are not evaluated or exempted from
missing-cache/error checks.

Unknown regions, ambiguous leaf names, cross-boundary merges, untitled second
schemas, numeric/date/formula header candidates, and unsupported layouts fall back
to the existing conservative parser and gate. This is not arbitrary spreadsheet
layout understanding. Single-row manual header overrides are rejected for verified
logical regions instead of silently ignoring the requested change.

## Nonblocking source disagreements

When a metric and a severity-category distribution disagree, both are preserved.
`SOURCE_SUMMARY_MISMATCH` information is attached to catalog ingestion notes, not
the import-warning confirmation gate. Composite populations are not compared as
one category. The Agent prompt distinguishes independent regions and requires
relevant source disagreements to be explained rather than silently corrected.
This is a bounded deterministic check, not a general business-audit engine.

## Verification and rollout

Both user-designated real workbooks were parsed, validated, materialized into
temporary SQLite databases and catalogued without cloud calls or live-session
changes. Both produced zero validation errors and zero import warnings. Their
summary tables contained respectively 9/29/63 and 4/3/2/3/25 records. Two real
source inconsistencies were retained as informational notes.

Synthetic regressions cover file-to-SQL-to-catalog behavior, attachment automatic
readiness, metric units, absent formula caches, two/three-level headers, record
swallowing, different-schema contamination, coverage loss, derived-value tampering,
notes, repeated headers, totals, and legacy single-table behavior.

The full Windows suite still has pre-existing SQLite temporary-file sharing
errors, a stale multi-workbook API expectation, and unavailable PDF native
libraries. It must not be reported as fully passing.
Final focused regression: 107 tests passed; core layout/integrity regression:
46 passed. Final full run: 213 tests, one failure, 23 errors, one skipped.
Logs are local-only under `data/structured-layout-*-tests*.log`.

Independent review: Standards found two issues (note-row classification drift and
per-column repeated fact scans); both were fixed and re-reviewed. Spec found two
blocking risks (untitled schema contamination and merging away the first business
record); regression cases now reject those layouts instead of certifying them.
The follow-up textual-record case also passes the conservative fallback check.

Existing attachments are not silently rebuilt. Re-upload the source workbook to
use the new layout parser; existing conversation data and original files remain
unchanged. No sensitive original workbook is added to Git or the Docker image.
