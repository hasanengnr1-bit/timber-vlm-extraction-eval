# Timber VLM extraction: evaluation code

Code for the study *Inference Depth and Error Detection in Vision–Language Model Extraction of Member-Level Design Parameters from Timber Drawings* (working title).

This repository holds only the parts of the SmartStructs backend that the study evaluates. It is a reference copy, not a standalone app: the extraction files still import modules from the full backend (database, logging, task queue).

## Contents

| Folder | What it is |
|---|---|
| `pipeline/prompts/` | The seven subsystem prompts (roof, floor, wall, post, footing, shear wall/lateral, project context) and the prompt loader |
| `pipeline/schema/schema.py` | Pydantic output schema (17 component categories, field-level `uncertain_areas`) passed to Gemini as `response_schema` |
| `pipeline/extraction/visual_extractor.py` | Launches the seven concurrent Gemini calls on the whole drawing set (from `app/taskiq_task/utils.py`) |
| `pipeline/extraction/data_extraction.py` | Task that runs extraction and link resolution (from `app/taskiq_task/data_extraction.py`) |
| `pipeline/matching/link_resolver.py` | Reconciles footing–wall, footing–post and masonry–shear-wall links with an audit note per change |
| `pipeline/llm/clients.py` | Gemini client setup (key read from environment) |
| `timberparse_eval/` | Evaluation tool: extraction runs, baseline, scoring against ground truth, review UI |

## Setup

API keys are read from environment variables (`GOOGLE_API_KEY` or `GEMINI_API_KEY`, `GEMINI_MODEL_NAME`). No keys or project drawings are stored in this repository; evaluation results are excluded by `.gitignore`.

## Still to add for the study

- Configurations C1 (generic single prompt) and C2 (single prompt + domain instructions)
- Deterministic timber validator (frozen before testing)
- Inference-depth and perceptual/reasoning error labels in the evaluation tool
