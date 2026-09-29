# KB Finder Workflow

Status: **frozen** (v9). Change this file only if Phase 1 inspection reveals a real architectural surprise or a measured implementation result requires a scope correction. Plan reviews are closed: new ideas go into `NOTES.md`, not this file.

KB Finder maps messy IT issue descriptions to the right public Coyote OneStop Knowledge Base article. It is a sequential retrieval experiment, not a multi-agent app build.

```text
evaluation set
      ↓
shared /api/search
      ↓
one retrieval configuration
      ↓
metrics
      ↓
compare
      ↓
add complexity only if results justify it
```

> Measure the retrieval problem before adding AI to it. Protect the evaluation from contamination. Use one production search path. Keep each new technique only when the numbers show it helps.

**Pre-AfroTech goal:**

```text
MUST-HAVE: lexical checkpoint (Section 7)
           → semantic retrieval (Section 8)
           → clean hosted app (Section 10)
STRETCH:   query normalization (Section 9), then redeploy
```

Section 10 runs directly after Section 8. Section 9 runs only if time remains after the hosted must-have is live. Each stage must be committed and pushed before the next starts. If time runs short, ship the last fully completed stage. Lexical + semantic, hosted, with honest numbers is the AfroTech story. No hybrid, reranking, or Antigravity before AfroTech.

---

## 1. Tools and sources of truth

| Tool | Role |
|---|---|
| VS Code | Main workspace |
| Claude Code | Primary implementation: architecture, retrieval, API, hard debugging |
| Codex | Mainly a **reviewer** at checkpoints. Bounded tasks (tests, CI) only when files don't overlap with Claude Code's work |
| Antigravity | Polished UI only, after retrieval is settled |

Claude Code builds everything. Codex reviews at these checkpoints:

1. collector and parser (end of Phase 2)
2. `/api/search` and the no-logging path (end of Phase 4)
3. `run_eval.py` metrics logic (end of Phase 4)
4. API key handling, access gate, rate limiting, and cost protection (before deploying in Phase 7)

Pattern: Claude Code implements → Codex reviews → Claude Code evaluates findings → fix valid issues.

Codex does not read `.claude/settings.json`; it has its own sandbox and approval settings. So: run Codex in **read-only mode** for checkpoint reviews, and keep `eval/` out of its review scope unless it is reviewing the metrics logic, and give it write access only for the rare bounded task (confirm the exact setting in the installed Codex version). It reads the same `AGENTS.md` guardrails. Run the post-session `git status -- eval/ data/samples/ NOTES.md .claude/ .githooks/` check after every Codex session too. Never run Codex and Claude Code on the same files at the same time.

Runtime APIs: **OpenAI only** through one dedicated KB Finder API project. Use a separate KB Finder API key, prepaid credits, auto-reload off, and a project spend limit where available. Embeddings: `text-embedding-3-small`. Normalization: `gpt-6-luna`.

Sources of truth: GitHub history, `SPEC.md`, `AGENTS.md`, `WORKFLOW.md`, and the human-owned files in Section 2.

Sequential work is the default. Most experiments depend on the result of the one before.

---

### Session discipline

Each phase gets its own fresh Claude Code session: Phase 1, Phase 2, Phase 4, Phase 5, Phase 6 (if reached), and Phase 7. Phase 3 is mostly human work in a separate chat and needs no Claude Code session.

**Start of every session:** point the agent at `AGENTS.md`, `SPEC.md`, `WORKFLOW.md`, `NOTES.md`, `HANDOFF.md`, and the relevant section. It reports current repo state and its plan before touching anything, and waits for approval.

**End of every session:** the agent overwrites `HANDOFF.md` (what was built, what is left, anything surprising) and it gets committed. For any real decision, the agent drafts a `DECISIONS.md` entry in chat, and you write the final entry in your own words. Then run the post-session check (`git status -- eval/ data/samples/ NOTES.md .claude/ .githooks/`) and review the `HANDOFF.md` diff. The next session starts from `NOTES.md`, `HANDOFF.md`, and `DECISIONS.md`, not from an old chat.

`HANDOFF.md` is fully disposable and rewritten every session. `NOTES.md` holds anything that must survive across sessions (notes for later phases, ideas). Only you edit `NOTES.md`; agents read it but never write it.

---

## 1b. How search works

The LLM never picks the article. Results always come from real articles in Postgres, ranked by a score, so the system cannot invent titles or links.

```text
pasted issue text
   ↓
(optional) normalization: gpt-6-luna rewrites it into KB-style wording
   ↓
retrieval over all indexed articles in Postgres
   ↓
ranked list of real articles (title, category, link)
```

Retrieval modes:

- **Lexical:** Postgres full-text search. Text is split into words, reduced to base forms, filler words dropped, and articles are ranked by how well they match. Fast and free, but only matches words.
- **Semantic:** the query is embedded with `text-embedding-3-small` and compared against stored article embeddings using pgvector. Articles with the closest meaning rank highest, even with no shared words.
- **Normalized lexical / normalized semantic:** same as above, run on the LLM-rewritten query.

Same query in, same ranked list out (given cached normalization). That is what makes it measurable.

---

## 2. Human-owned files and protection

Protected files:

```text
eval/**
data/samples/urls.txt
data/samples/category_urls.txt
```

Agents may never create, edit, or delete them.

`NOTES.md` is also agent-protected (Claude Code deny rules, AGENTS.md, post-session check) but is **not** covered by the pre-commit hook, since you edit it often and a terminal-only confirmation would get in the way. Agents may read it.

**Read access to `eval/**` and `cache/**` is also denied** during implementation sessions, so implementation sessions do not unnecessarily see the eval queries or their normalized rewrites. The Phase 3b labeling session is the explicit exception: the read deny on `eval/**` is lifted for that session only, then restored. This reduces benchmark exposure; it does not guarantee the model never saw the queries. Agents may read `data/samples/*.txt`.

The agent never needs the real eval file: `SPEC.md` documents the `cases.json` schema with one fake example case. You run `run_eval.py` yourself, since its per-case output shows the queries; share only aggregate numbers with the agent unless you are debugging the eval script itself.

A `Read` deny does not stop `cat eval/cases.json` through Bash. It is friction, not a wall.

### Layer 1: Claude Code deny rules (primary boundary)

In `.claude/settings.json`. Confirm exact syntax against the installed Claude Code version. Intent:

```json
{
  "permissions": {
    "deny": [
      "Edit(eval/**)",
      "Write(eval/**)",
      "Edit(data/samples/urls.txt)",
      "Write(data/samples/urls.txt)",
      "Edit(data/samples/category_urls.txt)",
      "Write(data/samples/category_urls.txt)",
      "Bash(git commit --no-verify:*)",
      "Bash(git commit -n:*)",
      "Bash(git push --force:*)",
      "Bash(git push -f:*)",
      "Bash(git commit --amend:*)",
      "Bash(git rebase:*)",
      "Bash(git reset:*)",
      "Bash(git filter-branch:*)",
      "Bash(git -c:*)",
      "Bash(git config:*)",
      "Edit(.git/**)",
      "Write(.git/**)",
      "Edit(.githooks/**)",
      "Write(.githooks/**)",
      "Edit(.claude/**)",
      "Write(.claude/**)",
      "Edit(NOTES.md)",
      "Write(NOTES.md)",
      "Read(eval/**)",
      "Read(cache/**)"
    ]
  }
}
```

The last group stops the agent from skipping the hook (`git -c core.hooksPath=...`), repointing it (`git config`), editing the hook script, or editing its own permission settings.

Note: `-n` is the short form of `--no-verify`, so both are denied. Bash deny rules match on command prefixes and can be dodged by unusual flag orders, so review agent Bash commands that touch Git.

### Layer 2: pre-commit hook (human friction, not a security boundary)

The hook detects staged changes to protected files and asks for a typed confirmation read from the terminal (`/dev/tty`). If no interactive terminal is available, the commit is refused.

An agent's Bash session has no interactive terminal, so it cannot confirm. You type `yes`. Do not use an environment-variable override: an agent can set environment variables (`VAR=1 git commit`, `export`, `env`).

The hook lives in `.githooks/pre-commit`. You activate it yourself once in Phase 0 with `git config core.hooksPath .githooks`.

**The VS Code Source Control panel has no terminal either**, so it will refuse commits that touch protected files. Commit those changes from the integrated terminal instead. Other commits work normally from the panel.

This catches accidents and makes intentional human edits deliberate. It does not replace Layer 1. Layer 2b is the real backstop.

### Layer 2b: post-session check

Edit/Write deny rules do not stop Bash writes (`sed -i`, `cat >`, `git checkout -- eval`). You cannot block every pattern, so after every agent session run:

```bash
git status -- eval/ data/samples/ NOTES.md .claude/ .githooks/
```

Any change you did not make yourself gets reverted and investigated.

### Layer 3: eval hash

Every `run_eval.py` run logs the SHA-256 of `eval/cases.json` alongside its results, so any result can be tied to an exact version of the evaluation set.

---

## 3. Phase 0: Setup (about 1 evening)

1. Create the repo and push to a private GitHub remote.
2. Create `SPEC.md`, `AGENTS.md`, `WORKFLOW.md`, `DECISIONS.md`, `NOTES.md`, `HANDOFF.md`, `.gitignore` (include `.venv/`).
3. Update `SPEC.md`:
   - remove all SQLite/FTS5 references; PostgreSQL from the first retrieval slice, pgvector when semantic begins
   - article structure includes Question/Answer as well as Issue/Environment/Cause/Resolution
   - eval cases use `target_article_ids` (a list), not a single ID
   - document the `cases.json` schema with one fake example case, so agents never need the real file
4. Set up the protection layers from Section 2.
5. Install the Supabase CLI. Local Postgres + pgvector runs through it, so local migrations match what gets deployed.
6. Optional, your call: a one-line heads-up to your supervisor that you are building a personal tool over the public KB and will host a password-protected copy.

### DECISIONS.md

A short running log, one entry per real choice: what was decided, why, and what the numbers showed. Examples: Postgres FTS instead of BM25, whole articles instead of chunks, which mode became the default and by how much. Write entries as decisions happen, not at the end. **You write the final entries yourself**, from the agent's draft in chat, so they sound like you when you explain them in interviews.

### Initial AGENTS.md

```markdown
# Project Guardrails

## Context
Retrieval tool mapping IT issue descriptions to public Coyote OneStop
Knowledge Base articles. Full spec: SPEC.md. Workflow: WORKFLOW.md.
Retrieval quality comes before UI polish.

## Current Stack
- Python 3 for KB inspection and collection

Planned later: Next.js, React, strict TypeScript, PostgreSQL, pgvector,
Python for offline ingestion and evaluation orchestration.

## Commands
- python -m venv .venv
- pip install -r requirements.txt
- python scripts/inspect_kb.py

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
```

---

## 4. Phase 1: KB inspection (about 1 evening)

### Human inputs

`data/samples/urls.txt`: about 10 articles across Accounts, Duo, Network/WiFi, Email, and Printing. Mix structured and simple articles. One distinctive body sentence each:

```text
<url> | <distinctive body sentence>
```

`data/samples/category_urls.txt`: two category pages.

- Software and Applications (617 articles, tests pagination and nesting)
- one small category

### Known from a manual check of article 8925

- Body is server-rendered in plain HTTP.
- Headings are Question / Answer.
- Meta tags carry category (`article:section`), published and modified time, and a description.
- A Related Articles section includes other articles' titles and summaries. It must be excluded from body text.
- A print route exists: `/TDClient/33/Portal/KB/PrintArticle?ID=<id>`.

### Inspection prompt

```text
Read AGENTS.md, SPEC.md, and WORKFLOW.md first.

This is Phase 1: KB inspection. No app code exists yet.
Do not scaffold Next.js. Do not create a database. Do not crawl.

Before writing code, report:
- current repo contents
- files you will create and why
- libraries you will use and why
Wait for my approval before implementing.

INPUTS (written by me, never edit):
- data/samples/urls.txt        format: <url> | <body phrase>
- data/samples/category_urls.txt

REQUEST RULES
- Fetch robots.txt once, check every URL with urllib.robotparser.
- Skip and report disallowed URLs. If the whole KB path is
  disallowed, stop and report.
- Descriptive User-Agent, 2-second delay, no retries.
- Report failures without stopping the run.
- Only fetch supplied URLs plus the PrintArticle version of each
  article (/TDClient/33/Portal/KB/PrintArticle?ID=<id>).
  Do not follow any other links.

ARTICLE INSPECTION
1. Save raw bytes:
   data/samples/html/<id>.html
   data/samples/html/<id>_print.html
2. Server-rendered test: strip HTML tags to text first, then
   decode entities, Unicode-normalize, collapse whitespace, trim.
   Do the same to the phrase. Phrase found = server_rendered true.
   No other heuristics.
3. For both normal and print versions report:
   - title, h1-h4 headings in order, article ID
   - meta tags: article:section, published_time, modified_time,
     og:description
   - tags if present
   - approximate candidate main-body text length, and the
     selector/DOM structure used to isolate it
   - heading pattern: Question/Answer,
     Issue/Environment/Cause/Resolution, other, none
   - whether a Related Articles section exists and how to
     exclude it from body text
   - which version (normal or print) is easier to parse cleanly
4. Recommend one body selector that works across all samples,
   or report where it breaks.

CATEGORY INSPECTION
1. Save raw bytes to data/samples/category_html/.
2. Report:
   - whether article links appear in raw HTTP
   - count of unique article IDs found
   - example URLs and whether titles accompany them
   - pagination: how it works, plain links vs postbacks/JavaScript
   - nested subcategory links
   - whether this looks stable enough for full collection

REPORTS
Write data/samples/inspection_report.md and .json.
Keep article findings and category findings separate.
Summarize: all bodies server-rendered or not, consistent vs
varying fields, whether discovery works with plain HTTP,
recommended body selector, parsing risks.

If any article fails the server-rendered test or discovery
fails: say so prominently, do not switch to Playwright,
do not change architecture, stop at reporting.

MAY TOUCH
scripts/inspect_kb.py, requirements.txt, data/samples/html/,
data/samples/category_html/, data/samples/inspection_report.*,
.gitignore

MUST NOT TOUCH
data/samples/urls.txt, data/samples/category_urls.txt,
eval/**, anything else

AT COMPLETION
- show the actual diff
- show the first 40 lines of inspection_report.md
- list anything unexpected
```

### Decision gate

```text
bodies server-rendered?        yes → plain HTTP parser
                               no  → stop, rethink ingestion

category pages list articles?  yes → plain HTTP discovery
                               no  → stop, rethink discovery
```

Browser automation is only ever an explicit decision, never a silent switch. This is the one gate that may reopen this workflow.

Commit the inspection fixtures while the repo is private. Reconsider if the repo goes public.

---

## 5. Phase 2: Collect and parse the Technology KB (about 1 to 2 evenings)

About 1,200 articles. At a 2-second delay, a complete first pass includes roughly 40+ minutes of request delay alone. The collector is resumable, and later runs may retry prior failures.

### Collector (`scripts/collect_kb.py`)

- Walk Technology categories and subcategories using the discovery method from inspection.
- Dedupe by article ID (articles can appear in multiple categories).
- Respect robots.txt, descriptive User-Agent, 2-second delay.
- No automatic retry within a run. A later run may retry previous failures.
- Validity check uses the inspection-derived body selector (lightweight, not the full parser).
- Write progress incrementally to a manifest:

```text
article_id
url
http_status
saved_path
parse_valid
body_length
fetched_at
error
```

Resume rule. Skip an article only when all three hold:

- previous fetch returned HTTP 200
- saved response exists
- body selector extracted a non-empty main body (`parse_valid = true`)

End of run: print success and failure counts.

### Parser

Built immediately after the collector works.

- Main article body only. Exclude nav, Related Articles, feedback widgets.
- Metadata from meta tags.
- Split Question/Answer or Issue/Environment/Cause/Resolution sections when present.
- Deterministic: same HTML in, same output out.
- Regression tests run against the Phase 1 fixtures.

---

## 6. Phase 3: Evaluation query set, then evaluation set (about 2 to 3 hours of human work)

Terminology:

```text
evaluation queries → frozen → relevance labeling → evaluation set
```

Phase 3a can overlap with Phases 1 and 2.

### 3a. Draft and freeze queries

1. In a separate plain chat with **no repo or KB access**, give the drafting LLM only short scenario topics (for example "student got a new phone, Duo stopped working"). The drafting LLM must be Claude or Gemini (not an OpenAI model, since OpenAI does normalization), and must not see KB content, retrieval results, or normalization prompts. Ask for how a stressed student would type it, plus a short technician version.
2. Rewrite or edit each one in your own words. Add a few sloppy ones yourself, by voice if faster.
3. Target size: 20 paired cases + 5 negatives (issues the KB should not cover).
4. Write `eval/cases.json` with `target_article_ids` empty:

```json
{
  "id": "case_001",
  "customer_query": "...",
  "technician_query": "...",
  "target_article_ids": [],
  "has_match": true
}
```

5. Commit and **push immediately**:

```text
test: freeze initial evaluation queries
```

Never reword a query after this point to improve retrieval.

### 3b. Relevance labeling

```text
agent proposes 3–5 candidate articles per case
(from titles/categories, NOT KB Finder's own search)
      ↓
you review candidates
      ↓
if none fit, search/browse the native KB manually once
      ↓
assign one or more target_article_ids
(or leave empty and has_match false for negatives)
      ↓
note any case where the answer was not in the suggestions
```

Agents propose candidates in chat output only. You edit `cases.json`. This is the only session where the `Read(eval/**)` deny is lifted. Restore it before the next session.

Commit separately:

```text
test: assign reviewed target article IDs
```

The pushed commit order proves wording existed before labels.

---

## 7. Phase 4: Lexical loop and checkpoint (about 3 to 4 evenings)

1. Scaffold Next.js, React, strict TypeScript. Local PostgreSQL via the Supabase CLI.
2. Update `AGENTS.md` with commands that now exist.
3. Ingest all parsed articles into Postgres.
4. Minimal contracts:

```ts
type Article = {
  id: string;
  sourceArticleId: string;
  title: string;
  url: string;
  category?: string;
  tags: string[];
  fullText: string;
  question?: string;
  answer?: string;
  issue?: string;
  environment?: string;
  cause?: string;
  resolution?: string;
};

const SearchRequestSchema = z.object({
  query: z.string().min(1).max(5000),
  mode: z.literal("lexical"),
});
```

Do not pre-create other modes.

5. `POST /api/search` using Postgres full-text search. Label it "Postgres FTS baseline" (it is not BM25).
6. Bare UI: one textarea, one button, a results list. No design work.
7. `scripts/run_eval.py`:
   - calls `/api/search` only, simple asserts on the response shape
   - logs the SHA-256 of `eval/cases.json`
   - reports Track A (customer) and Track B (technician) separately
   - positive cases: Top-1, Top-3, MRR, with raw counts (for example `14/20 (70%)`)
   - negative cases: top returned article, top score for the mode (lexical score, or top similarity for semantic), ranked list, and your qualitative judgment. No precision/recall until an abstention mechanism exists.
   - a hit means any ID in `target_article_ids` appears
8. Confirm request bodies are never logged or stored.

### Checkpoint (AfroTech goal)

```text
full KB collection → deterministic parser → all articles in Postgres
→ lexical /api/search → bare UI → frozen and labeled 20+5 evaluation set
→ reproducible metrics → README
```

Agent verifies (in session):

```bash
npm run lint
npm run build
npm run test:unit
```

plus a smoke test of `run_eval.py` against the fake example case from `SPEC.md`.

You verify (after the session, yourself):

```bash
python scripts/run_eval.py --mode lexical --cases eval/cases.json
```

`--cases` is required with no default, so nothing can silently run against the real set. Every real eval run passes `--cases eval/cases.json`; the agent's smoke test passes `--cases tests/fixtures/fake_cases.json`. Share only aggregate numbers with the agent. The same split applies to every later `run_eval.py` run.

Commit and push:

```text
feat: establish retrieval evaluation loop
```

### README must include

- what the tool does and the candidate pool size
- the evaluation method, with this provenance statement:

> Evaluation queries were initially drafted by an LLM without access to the Knowledge Base, then rewritten or edited by me before target articles were identified. Query wording was frozen in Git before relevance labels were assigned.

- first lexical numbers for both tracks, raw counts included
- a clear note that 20 cases is small and small differences mean little
- negative cases shown qualitatively
- a data note: semantic and normalized modes send query text to OpenAI; issues are entered manually without personal details
- what comes next and why

---

## 8. Phase 5: Semantic search (about 1 to 2 evenings)

Starts only after the Section 7 checkpoint is committed and pushed.

### Provider

Embeddings: **OpenAI `text-embedding-3-small`** (default). Anthropic has no embedding model. Swap only if the eval gives a reason; switching models means re-embedding the whole KB.

### Steps

0. **Cost protection first, before any paid call:** create the dedicated KB Finder OpenAI project and a separate API key, load prepaid credits ($5 is enough), turn auto-reload off, and set a project spend limit where available. Key goes in `.env.local` only (`.env*` is gitignored), never in code.
1. Enable pgvector. Add an embeddings table with `embedding_model`, `embedding_template_version`, `embedding_dimensions`, `embedded_at`.
2. Define the article template once, versioned (`article-v1`): title, question/issue, body. Whole articles first. Chunk only if the eval shows long articles being missed. Write `article-v1` before looking at per-case semantic results, and do not tune it against the eval cases.
3. Embed all articles in one offline batch. Log token count and cost.
4. Add `semantic` mode to `/api/search`. Mode rules, enforced server-side:
   - `DEFAULT_SEARCH_MODE` env var sets the app's default mode and is always allowed.
   - `EVAL_MODES_ENABLED=true` unlocks the other modes (for eval runs).
   - `/api/search` rejects any mode that is neither the default nor unlocked.
   - Query embedding config must match stored config or the request fails loudly.
5. **Pre-register the decision rule** in `DECISIONS.md` and commit it before any semantic eval run. Default rule: semantic becomes the default only if it beats lexical on customer-track Top-3 by at least 3 cases and is not worse on technician-track Top-3. Otherwise lexical stays. With 20 positive cases, 3 cases is a 15-point swing: this is a pragmatic decision rule, not statistical significance.
6. You run `run_eval.py --mode semantic --cases eval/cases.json` and compare against lexical, both tracks, raw counts. Eval query embeddings are cached in `cache/embeddings/`, keyed by query hash, embedding model, and dimensions (store the hash, not the query text), so repeated eval runs give identical rankings.
7. Apply the pre-registered rule to pick lexical or semantic as the default. Record the choice and the numbers in `DECISIONS.md`. Set `DEFAULT_SEARCH_MODE` accordingly.
8. Commit and push: `feat: add semantic retrieval mode`.

With 20 cases, only trust large differences.

---

## 9. Phase 6: Query normalization (about 1 to 2 evenings)

**Stretch goal.** Starts only after Section 10 (hosted lexical + semantic app) is live, committed, and pushed.

### Provider

**OpenAI `gpt-6-luna`** rewrites the raw query into short KB-style wording. Reasoning effort `none` or `low` (short rewrite task, faster, cheaper). Luna is a reasoning model, so temperature 0 may not be available; reproducibility comes from the cache, not the model settings. Record the exact model name returned in each response, since aliases can point to newer versions over time.

Before starting, confirm the exact model names (`gpt-6-luna`, `gpt-6-sol`) and supported parameters in the OpenAI console, and pin them in config. Model lineups change quickly.

### Steps

1. Normalization prompt lives in one versioned file (`normalize-v1`). **Anti-leak rule:** write `normalize-v1` without looking at per-case eval results, and do not revise it to make specific eval cases pass. Only one prompt version ships before AfroTech. Prompt iteration waits until the expanded eval set exists after AfroTech.
2. Eval runs use a cache in `cache/normalization/` (outside `eval/`), keyed by case, track, model, and prompt version. Store original query hash, output, model, prompt version, timestamp. Reuse unless intentionally testing a new config.
3. Add `normalized_lexical` and `normalized_semantic` modes, gated.
4. Compare all four: lexical, semantic, normalized lexical, normalized semantic. Both tracks, raw counts.
5. If normalization does not beat raw queries, rerun the same cached-case set once with `gpt-6-sol` (costs cents). This separates "normalization does not help" from "Luna was not strong enough." Report both runs.
6. Commit and push: `feat: add query normalization experiment`.

The cache is an experimental artifact, not ground truth. Original queries are never rewritten.

If a normalized mode clearly wins, record it in `DECISIONS.md`, update `DEFAULT_SEARCH_MODE`, and redeploy. If normalization does not help, the default stays as chosen in Section 8.

---

## 10. Phase 7: Clean UI and hosting (about 1 to 2 evenings)

Runs directly after Section 8. If Section 9 is completed later, redeploy with the measured best mode.

### UI

Tailwind + shadcn/ui. Clean and readable, not a full design pass: search box, result cards with title, category, snippet, and link to the original article. Must not touch `lib/search/`, `scripts/`, or `eval/`.

### Hosting

| Piece | Service | Cost |
|---|---|---|
| App + API | Vercel Hobby | Free, personal non-commercial use |
| Database | Supabase free tier (Postgres + pgvector) | Free; pauses after 7 days idle, so check it before demos |
| Embeddings + normalization | OpenAI, prepaid credits | Cents |

At this project scale, expected API usage is very small: embedding roughly 1,200 KB articles and running dozens to hundreds of short normalization calls should normally cost far less than the initial $5 prepaid balance. The main financial risk is accidental repeated calls or unrestricted public traffic, not normal evaluation use.

### Cost and key protection

Already in place from Phase 5 step 0: prepaid credits with auto-reload off, dedicated OpenAI project, separate key, spend limit. Do not treat prepaid exhaustion as an instantaneous hard cutoff: billing enforcement can lag slightly, so the spend limit, access gate, and rate limit are layered safeguards.

Required before deploying:

1. **Verify Phase 5 protection** is still set (auto-reload off, spend limit on).
2. **Dedicated key stays dedicated**: the same KB Finder key, not a shared one.
3. **Keys server-side only**: Vercel environment variables, never `NEXT_PUBLIC_`, never committed. `.env*` in `.gitignore`.
4. **Access gate**: the deployed app requires a password (middleware checking a secret env var) so strangers with the URL cannot run searches on your credits. `run_eval.py` runs against local with the gate off, never against production.
5. **Rate limit** `/api/search` per IP using a small Postgres table in Supabase (IP, window start, count). Not in-memory: Vercel serverless instances do not share memory, so in-memory counters barely limit anything. Delete windows older than one day so the table never builds up IP history.
6. `EVAL_MODES_ENABLED=false` and `DEFAULT_SEARCH_MODE` set in production. The app uses only the chosen default mode.

### Final pre-AfroTech state

```text
MUST-HAVE:
full KB + deterministic parser
→ lexical baseline
→ semantic comparison
→ best measured mode selected
→ hosted, password-protected app
→ README with lexical vs semantic results
→ DECISIONS.md up to date

STRETCH:
→ normalization comparison, redeploy if it wins
```

---

## 11. After AfroTech

```text
expand eval to 40+ paired cases (same freeze → push → label process)
      ↓
rerun all modes on the larger set
      ↓
subset-free native baseline (full KB is already indexed)
      ↓
no-match threshold
      ↓
hybrid, only if justified
      ↓
reranking, only if justified
      ↓
periodic KB refresh (rerun collector, re-embed changed articles,
record corpus snapshot date with every eval run)
      ↓
polished UI pass in Antigravity
```

### No-match threshold

Do not pick a threshold by eyeballing 5 negatives. Tune on the expanded set, report how it was chosen, then report no-match precision and recall. Do not present similarity scores as probabilities.

### Real issue text

Issues are typed in manually with names, IDs, emails, and phone numbers left out. Request bodies are never logged or stored.

### UI polish phase

```bash
git worktree add ../kb-finder-ui -b ag/product-ui
```

Antigravity may touch `app/`, `components/`, `styles/`, `DESIGN.md`. It must not touch `lib/search/`, `scripts/`, `eval/`, or retrieval migrations, and must use the existing `/api/search` contract.
