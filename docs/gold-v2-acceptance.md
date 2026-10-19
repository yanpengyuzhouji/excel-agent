# Golden v2 acceptance protocol

The private suite contains 64 model turns: 37 retained baseline questions, 22
complex questions, a three-turn conversation (ending in a report), and two
standalone report questions. Three report turns cover HTML and PDF output.

Coverage includes filtered aggregates, rankings, shares, weighted averages,
deduplication, compound conditions, missing-data refusal, source contradictions,
unit conversion, exact-key cross-workbook matching, missing join keys, planned-year
comparisons, conversational scope retention, report preview and PDF download.
The cross-workbook CSV fixtures are controlled derivatives of a real technical
workbook, not additional independent business samples.

## Execution

Run `scripts/build_gold_v2.py` once to freeze the next private suite. It refuses
to overwrite an existing version. Source coordinates and independently calculated
Decimal expectations are recorded under the ignored private data directory.

After approving business-data transmission and model costs, run
`scripts/run_gold_v2.py` with the existing test UI on port 8092 and the core
container on port 8091. It uses the configured DeepSeek model, at most two model
requests concurrently, and does not automatically resubmit failed questions.
The service itself may recover a no-body response once under the same deadline
and Agent budget; capture `model-diagnostics.json` with
`scripts/collect_model_diagnostics.py RUN_DIRECTORY` before replacing the core
container. Report provider attempts and recovery attempts separately from the
logical question results.

The runner checks whole-file admission, catalog coverage, paginated SQL rows and
cells, unsupported file rejection, model evidence and final answers. Report cases
also fetch HTML/PDF, extract PDF text, render PDF pages, and exercise the
real frontend history preview and PDF download with Playwright. Visual acceptance
requires a separate human/agent inspection of the saved page images and previews.

Only temporary conversations created by this run are deleted. Cleanup outcomes,
container image hashes before/after, suite hash, SSE traces, initial failures and
report artifacts are retained in the private run directory. Do not rebuild or
modify the application during a baseline run.

## Interpretation

`auto_pass_pending_semantic_review` is screening, not business certification.
Numeric presence alone cannot establish correct metric-to-label association,
units, absence of unsupported claims, or layout quality. Strict text/ranking/pair
checks can also produce false negatives. Record semantic adjudication separately
without rewriting the initial automated result. Model transport failures must be
reported separately from wrong answers and import failures.

The first v2 baseline exposed formatting false negatives for space-grouped numbers
and full-width colons. The checker now normalizes those forms and checks explicit
refusal content. Supplemental checks on saved answers must remain separate from
the initial result; they require no new model call. Seven synthetic checker tests
cover these rules. An unsuccessful future runner execution exits nonzero.

Business files, expected values, traces and generated reports must never enter
Git or the Docker build context. Formal release still requires business-owner
signoff, resource/concurrency acceptance and operational recovery validation.
