# TimberParse Eval

A local review application for the existing SmartStructs VLM extraction pipeline. It evaluates extraction and existing uncertainty flags, not structural calculations. The original application is untouched. No PostgreSQL, Redis, authentication, or payment service is needed.

## Start

Python 3.11+ is required. From this directory in PowerShell:

```powershell
./setup.ps1       # first installation only
./start.ps1
```

Open http://127.0.0.1:8765. This workspace already has an installed `.venv`.

Choose a different results folder with the **Results folder** button, or:

```powershell
./start.ps1 -Results 'F:/My evaluation results' -Port 8765
```

Keep the server running while reviewing. Use one server process per results folder. Stop with Ctrl+C. On restart, unfinished extraction runs are marked interrupted; start a new run rather than overwriting them.

## Extraction configuration

The adapter reads `app/.env` without exposing it to the browser. Set `GOOGLE_API_KEY` (or `GEMINI_API_KEY`) and `GEMINI_MODEL_NAME` there or in environment variables. Optional link reconciliation uses `GEMINI_LITE_MODEL_NAME`, falling back to the original resolver's model default.

**Run extraction → Start extraction** is the only action that calls a provider and may incur charges. It uploads the PDF to Google's file API, makes the same seven schema-constrained calls with the original prompt text and user instruction, and optionally applies the original link-resolution logic. Provider files are deleted after completion where possible. No paid call was used during development.

The adapter imports the actual schema classes and reads the actual prompt files. It replaces production queue/database infrastructure with local file storage, captures responses and usage, and exposes call failures. It does not call calculators or hazard APIs. Source snapshots and hashes record the baseline used. Generation settings not set by the original pipeline remain provider defaults. These are not guarantees of deterministic model output.

For offline review, import the original extraction JSON object (top-level keys such as `roof_system`, `floor_system`, `wall`). Imported output is labeled with unverified provenance; snapshots describe the local schema, not the unknown original run. Fields outside the existing schemas are rejected. Partial original-schema outputs are allowed so errors and omissions can be reviewed.

## Review

1. Upload a drawing-set PDF or choose **Try a synthetic example**. Demo plans and deliberate mistakes are clearly labeled and are not research data.
2. Run extraction or import saved JSON. Select the run and raw/reconciled stage. Each stage has independent annotations.
3. Mark each predicted member **present**, **extra**, or **duplicate**. One collection object is one annotation unit, including a repeated joist group.
4. Select a field and mark **correct**, **incorrect**, **unsupported**, **cannot determine**, or **N/A**. Unreviewed is the default. Record corrections and review notes separately from immutable predictions.
5. Drag on the PDF to attach an evidence rectangle to the selected field. Page-relative coordinates survive zooming. Multiple rectangles/pages are supported. Markings can be removed; previous annotation revisions retain them.
6. Add missed members in an existing collection, with evidence page, notes, and optional properties from its schema. Use **Member inventory** to confirm completeness only after checking every predicted member and adding all missed members.
7. Review **Accuracy & uncertainty**. Export a portable ZIP containing the source PDF, run metadata, predictions, snapshots, annotations/history, markings, scores, and CSV.

Keyboard shortcuts outside text inputs: **1** correct, **2** incorrect, **3** unsupported, **4** cannot determine, **5** N/A, **N** next field. Autosave shows its state in the run bar. Do not close the page while a save is pending; failed saves can be retried. Concurrent editing conflicts are rejected rather than overwriting a newer revision.

## Uncertainty and metrics

`uncertain_areas` is interpreted at its original object scope. An exact field name in the list flags that field. An explicit list without that name marks it unflagged. An absent/null list means unknown. Object-level flags are not expanded into child-field flags. Original notes remain visible. A reviewer can explicitly map an existing free-text flag to a field, with the override and rationale saved separately; this is not a newly generated confidence score.

Notes, `uncertain_areas`, and fields explicitly marked `SKIP_EXTRACTION` in the source schema are excluded from field accuracy. Nested objects are expanded; scalar and object arrays are reviewed as whole field values. This is a manual field evaluation, not a geometric localization or per-array-element benchmark.

- Field accuracy = correct / (correct + incorrect + unsupported).
- Errors = incorrect + unsupported. Indeterminate, N/A, and unreviewed cases are excluded.
- Error-flag recall = flagged errors / errors **with known field-level flag status**.
- Flag precision = flagged errors / eligible flagged fields.
- Unflagged error rate = unflagged errors / eligible unflagged fields.
- Unknown flag status is excluded from flag metrics and counted explicitly.
- Fields belonging to extra/duplicate members are excluded from field accuracy; those members remain false positives in member precision.
- Member precision = present / (present + extra + duplicate).
- Member recall = present / (present + missed).
- Member scores require a completed collection inventory and a member verdict for every predicted object. Missing-member fields do not enter matched-field accuracy.
- Zero denominators yield N/A. Partial or selectively reviewed scores do not estimate whole-dataset performance. Failed extraction calls remain visible and are not converted into empty successful systems.

Manual verdicts control scoring. Corrections are text records, not an automatic numerical-tolerance evaluator. Evaluate the same predefined fields and complete project scopes consistently for a paper.

## Saved files

```text
evaluation_results/
  project_<id>/
    project.json
    drawings/input.pdf
    runs/run_<id>/
      run_metadata.json
      extraction_raw.json
      extraction_reconciled.json       # only when requested
      schemas.json
      exclusions.json
      prompts/
      baseline_source/
      responses/                      # live runs only
      annotations/
        raw/
          reviews.json                # authoritative annotation state
          history/revision_000001.json
          missing_members.json
          pdf_markings.json
          scores.json
          review_export.csv
          summary.txt
        reconciled/                   # independent review, when used
```

Writes use temporary files and atomic replacement. `reviews.json` is the source of truth; derived exports are regenerated after saves and on export. History snapshots preserve previous corrections and markings. Paths inside projects are relative; move/copy complete project folders into another results root and select that root to reopen them. API keys are never included in snapshots. Provider response files may contain drawing content, so treat the entire folder as project data.

## Verification

```powershell
./.venv/Scripts/python.exe -m pytest tests -q
node --check static/app.js
```

Tests cover metric denominators, flag scope, missing/duplicate members, annotation persistence, history, stale-edit rejection, exports, PDF rendering, stage isolation, source-prompt snapshots, resolver guards, and local-origin restrictions. Live Google model behavior still requires a user-triggered run with working credentials and model access.
