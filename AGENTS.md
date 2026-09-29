# Project Guardrails

## Context
Retrieval tool mapping IT issue descriptions to public Coyote OneStop
Knowledge Base articles. Full spec: SPEC.md. Workflow: WORKFLOW.md.
Retrieval quality comes before UI polish.

## Current Stack
- Python 3.12 (in .venv) for KB inspection and collection

Planned later: Next.js, React, strict TypeScript, PostgreSQL, pgvector,
Python for offline ingestion and evaluation orchestration.

## Commands
- py -3.12 -m venv .venv
- .venv/Scripts/python -m pip install -r requirements.txt
- .venv/Scripts/python scripts/inspect_kb.py
- .venv/Scripts/python scripts/collect_kb.py [--limit N] [--refresh-categories]
- .venv/Scripts/python scripts/parse_kb.py
- .venv/Scripts/python -m unittest discover -s tests

Always run Python through .venv/Scripts/python. Never use bare
python or python3 (python3 on this machine is 3.9).
Add commands only after they exist. Do not invent command names.

## Retrieval Rules
- All production retrieval goes through POST /api/search.
- run_eval.py must call /api/search. Never reimplement ranking in Python.
- Never invent article titles, IDs, or URLs.
- Do not add normalization, semantic, hybrid, chunking, or reranking
  until that experiment begins.
- Experimental modes must be gated server-side.

## Human-Owned Files
- Never create, edit, or delete eval/**, data/samples/urls.txt,
  data/samples/category_urls.txt, or NOTES.md.
- Never read eval/** unless the session is explicitly the
  labeling session. Use the schema in SPEC.md instead.
- Never use git commit --no-verify or -n, git push --force,
  git commit --amend, git rebase, or git reset.

## Data Handling
- Public KB article content may be stored.
- Issue/ticket text must stay ephemeral. Never persist or log
  request bodies containing issue text.
- Do not store names, IDs, emails, phone numbers, or ticket data.
- Semantic and normalized modes send query text to OpenAI.
  This is an accepted decision: issues are typed in manually
  with names, IDs, emails, and phone numbers removed.

## Verification
Before reporting work complete:
- run the relevant command (except run_eval.py, which the human runs;
  use the smoke test against the fake SPEC.md case instead)
- report failures
- show the actual diff
- do not claim success from code inspection alone