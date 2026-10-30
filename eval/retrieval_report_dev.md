# Retrieval tune: dev report (F2 tune attempt 3)

Companion to `eval/retrieval_report_dev.json` (sha256 `3ee98066cfadbd38998e26eee8159dc841ed35fbf834bc0982110fc1a819788b`) and `eval/retrieval_params.json` (sha256 `e332d59a205d79a92c3ddabc58fd0f05c4f8b53f57eaaa32cdf1206555f2d325`).

Internal: dev numbers are not for the landing page or demo script.

## Chosen setting

- `k_tables=5`, cutoff (`max_distance`) 0.75
- Split: dev only, n=40 (chinook 20, clinic 20)

## Coverage@k at the chosen setting

Counts are rate × n from the JSON report; each is an integer. The intervals are Wilson 95% (z=1.959964).

| Scope | Covered / n | coverage@k | Wilson 95% |
|---|---|---|---|
| Overall | 37/40 | 0.925 | 0.801–0.974 |
| chinook | 20/20 | 1.000 | 0.839–1.000 |
| clinic | 17/20 | 0.850 | 0.640–0.948 |

## Caveats

- The cutoff does not bind on dev. At k=5, cutoffs 0.75, 1.01 and None tie (coverage 0.925, recall 0.9667), so 0.75 was picked by the tie-break (stricter cutoff) and is unvalidated.
- k=5 is the top edge of the {1, 3, 5} grid.
- These params are dev-tuned, not validated.
- The tune recorded no per-question results, so the IDs of the missed clinic questions are not available. They will come with the ADQ-12 per-question records.

## Provenance (from the JSON report)

- `image_digest`: `sha256:ba3fcc19ed69dd31faaca0f4eb72f561d392c06f9fbace08c3f7fc87cca59335`
- `requirements_lock_sha256`: `3bff36bd67d14b70280c035616a43522ffaaaa4657db001c94e85ed7c586e4db`
- `clinic_content_hash`: `b845161b9507434424c13a50f97f839d74a032acee89242a51ff2b25795e020c`
