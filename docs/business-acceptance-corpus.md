# Private real-business acceptance corpus

Collected on 2026-10-15 from the user-designated local directory. Source files
were read only; no application imports, cloud model calls, conversions, formula
recalculations, or Git uploads were performed.

The private output is `data/business_acceptance/2026-10-15/`, excluded from Git;
the entire `data` directory is also excluded from Docker build context.

Contents:

- `files/`: content-addressed original copies, with verified SHA-256 hashes.
- `manifest.json`: full source-to-copy provenance, hashes, container signatures,
  format expectations, collection failures, and test/generated-artifact labels.
- `review-index.csv`: a local review index, not a business answer dataset.
- `acceptance-candidates.json`: a first-pass shortlist spanning business folders,
  small/large files, formats, and previously discussed business workbooks.

Initial inventory: 336 source files, 333 unique contents, three exact duplicates;
134 XLSX, 188 XLS, 12 CSV, and two WPS ET files. Collection failures: zero.
These counts describe files, not parsed records or successful application imports.
The first-pass shortlist contains 21 candidate cases. Thirteen source entries
under known test/generated-output folders are labeled separately, not treated as
primary real-business success cases. Labels are path heuristics requiring review.

Current application accepts XLSX/XLSM/CSV. XLS/ET remain unsupported-format
negative cases; collecting them does not add parsing support. Do not rename an
extension to simulate conversion. Any future conversion must preserve the
original, record the converter/version, and independently reconcile the result.

The corpus is NOT anonymized and is NOT a certified Gold dataset. Original data
and private manifests must not be committed, published, included in images, or
submitted to external model providers without explicit approval.

Before treating shortlisted cases as Gold:

1. Review and exclude generated reports, old acceptance outputs, and synthetic
   regression files from the primary business-success set.
2. Independently annotate sheet coverage, true business rows, excluded subtotal
   rows, identifier examples, units, and amount/count controls with coordinates.
3. Record business questions, expected outputs, and a reviewer. Do not derive
   expected values solely from the parser or Agent being tested.
4. Separate parser/SQL correctness, model answer correctness, resource testing,
   and explicit unsupported-format rejection. A parser pass is not an Agent pass.

Reproduce with PowerShell:

```powershell
./scripts/collect_business_acceptance.ps1 -SourceRoot '<authorized source directory>' -OutputRoot '<new private output directory>'
```

Use `-Resume` only with a prior collection. Existing copies are never overwritten
and their hashes are checked again. Collection allows shared read handles for
files open in Excel and rejects copy/source hash mismatches. Collection is not
a filesystem snapshot; for reproducible tests use the verified frozen copies.
