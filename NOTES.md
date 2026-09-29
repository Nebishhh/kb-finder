# NOTES

Human-owned. Agents read this file but never edit it.
Persistent notes for later phases and new ideas go here.

## Notes for later phases
- Phase 4: run_eval.py takes a required --cases <path> argument
  (no default). Agent smoke test uses
  tests/fixtures/fake_cases.json, which mirrors the SPEC.md
  schema example. Human runs:
  .venv/Scripts/python scripts/run_eval.py --mode lexical --cases eval/cases.json
- Claude Code 2.1.248: Write() deny rules are ignored; Edit() covers
  all file-editing tools. .claude/settings.json uses Edit only.

## Ideas