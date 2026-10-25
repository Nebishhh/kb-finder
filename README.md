# KB Finder

Type an IT problem the way a person would say it. Get the knowledge base articles that fix it.

KB Finder searches the 3,109 Technology articles in the University of South Dakota's public IT knowledge base. It returns real articles, ranked. It never writes its own answers.

**Live demo:** [kb-finder.nebanebish.com](https://kb-finder.nebanebish.com) (access code on request, to limit API costs)

**Stack:** Next.js 16 · React 19 · TypeScript · Python 3.12 · PostgreSQL + pgvector · OpenAI embeddings · Supabase · Vercel

![KB Finder search page](docs/screenshot.png)

## Why

I work at the USD IT Service Desk. People describe problems in their own words, like "my laptop screen broke." The knowledge base uses IT terms, like "Student Personal Computer Hardware Repair." Keyword search can struggle when the words don't match. KB Finder searches by meaning instead.

## How it works

- **Collect:** a Python crawler copies the public articles, following robots.txt with a 6-second delay.
- **Search:** articles are turned into embeddings with OpenAI and matched to the user's description in Postgres (pgvector).
- **Serve:** a Next.js app with one search API. The eval script calls the same API, so what gets tested is what users get.

## Results

I tested keyword search against semantic search on 21 real-style problems, each written two ways: how a student would type it, and short technician wording.

| | Correct article in top 3 (student wording) | Correct article in top 3 (technician wording) |
|---|---|---|
| Keyword search | 13/21 (62%) | 11/21 (52%) |
| Semantic search | 16/21 (76%) | 16/21 (76%) |

I set the pass bar after the keyword baseline but before any semantic results. Semantic cleared it, so it became the default. It cleared the student bar exactly, and 21 cases is a small set, so I treat this as a promising start, not proof. Full numbers and caveats are below and in [DECISIONS.md](DECISIONS.md).

## What I focused on

- **Honest evaluation:** test cases were locked before any results, the pass rule before any semantic results, and nothing was retuned after seeing numbers.
- **Privacy:** searches are never stored or logged.
- **Fail safe:** if semantic search breaks, the app shows an error instead of quietly giving worse results.
- **Cost and access limits:** an access code, a daily search cap, and a restricted API key.

## Known limits

- It always returns results, even for problems the knowledge base doesn't cover.
- The test set is small. A bigger one, ideally written by other people, is next.

## How it was built

I built this with AI coding agents under written rules. Claude Code writes the code, Codex reviews it, and every change waits for my approval. I read each diff and make every commit myself. The rules are in [AGENTS.md](AGENTS.md), and the full technical spec is in [SPEC.md](SPEC.md).

## Run it locally

The knowledge base content isn't included, but the tests run without it:

```
py -3.12 -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
npm install
.venv/Scripts/python -m unittest discover -s tests
npm run test:unit
```

Full setup is in the details below.

## License

Code: MIT. Knowledge base content belongs to the University of South Dakota and isn't covered by that license.

Personal project. Not an official USD tool.

---

## Details

<details>
<summary><b>How it works, in full</b></summary>

1. **Collect.** A Python crawler walks the public KB category pages over plain HTTP. It respects robots.txt and waits 6 seconds between requests. A full run takes about 5.3 hours.
2. **Parse.** Each article becomes structured fields: title, sections, tags. Text in "ITS Staff Only" boxes goes into its own field and is not searchable in the live app.
3. **Search.** Two modes, both in Postgres:
   - `lexical`: Postgres full-text search. This is the baseline.
   - `semantic`: OpenAI `text-embedding-3-small` over whole articles, with exact cosine search in pgvector.
4. **Serve.** A Next.js app with one search route, `POST /api/search`. The UI and the eval script both call it, so what gets measured is what users get.

The hosted app runs semantic mode on Vercel and Supabase.

</details>

<details>
<summary><b>Full results and caveats</b></summary>

21 positive test cases, each written two ways: Track A is how a stressed student would type it, Track B is short technician wording. Top-3 means a correct article is in the first three results. Depth 50 for every mode.

| Mode | Track | Top-1 | Top-3 | MRR@50 | No target in top 50 |
|---|---|---|---|---|---|
| lexical | A (student) | 7/21 (33%) | 13/21 (62%) | 0.481 | 1/21 |
| lexical | B (technician) | 8/21 (38%) | 11/21 (52%) | 0.489 | 1/21 |
| semantic | A (student) | 12/21 (57%) | 16/21 (76%) | 0.690 | 0/21 |
| semantic | B (technician) | 6/21 (29%) | 16/21 (76%) | 0.542 | 0/21 |

The set also has 4 negative cases: problems the KB shouldn't cover. They aren't in the table. With no abstention, semantic always returns something, so I review those by hand.

Semantic became the default because it passed a rule I wrote before any semantic run: Track A Top-3 at least 16/21 and Track B Top-3 at least 11/21.

**Read these numbers with care:**
- 21 cases is small. One case moves a metric by about 5 points.
- Track A met the bar exactly. One case the other way and semantic would have failed the rule.
- Semantic lost Top-1 on technician wording (8 to 6). Short queries that match a title still favor keywords.
- The two modes don't see the same fields (semantic has no tags or description), so the gap is not only the method.
- The first scenario topics were drafted with an LLM from my project scope and workflow notes, without browsing the KB. I can't rule out that those notes overlapped with the sample articles I'd inspected. Some cases came from drafts written without a topic list. I wrote or edited the final wording and froze it before assigning any labels.

The test cases are in `eval/cases.json`.

</details>

<details>
<summary><b>Engineering decisions</b></summary>

- **Pre-registered evals.** Queries frozen before labels, labels frozen before the first run, decision rule written before the semantic run. Nothing is retuned after seeing numbers. Any change is a new, logged experiment.
- **Pinned configs.** The corpus, migrations, embedding config and article embeddings are pinned by SHA-256. The search function refuses to run on missing, stale or mismatched embeddings.
- **Queries are never stored.** Query text, its hash and its embedding are never written to logs, files, caches or database tables. Errors log a fixed code, never request text. In semantic mode the query is sent to OpenAI to be embedded.
- **Fail closed.** A semantic failure returns an error, never a silent fallback to keyword results. A misconfiguration blocks search instead of weakening it.
- **Bounded cost and access.** A shared access code with a daily cap on failed logins, 200 searches per day, a request size cap, and a restricted embeddings-only API key with low prepaid credit.
- **Locked-down database.** The app connects as a least-privilege role with row-level security, over TLS with certificate checks. The public Data API is disabled.

</details>

<details>
<summary><b>Limitations and what's next</b></summary>

**Limitations**
- **No abstention.** Semantic always returns results, even for problems the KB doesn't cover. The UI says so.
- **Small eval set** (see above).
- **Semantic reruns can differ slightly.** Query embeddings aren't cached, by design.
- **Free-tier hosting** pauses after 7 idle days.

**What's next**
- **Eval v2.** About 50 new queries written blind, ideally by other people in their own words, with labels and the decision rule fixed before any run. It gates the next two items.
- **Staff-only text as a search signal.** Built locally as `article-v2`, not scored yet. It makes that text searchable but never shows it in results.
- **Comparison against the KB's built-in search.**
- **An "I'm not sure" path** instead of always returning results.

</details>

<details>
<summary><b>Full local setup</b></summary>

The current tree doesn't include the KB content, so a full setup starts by collecting it. That makes over 3,100 requests to USD's site over about 5.3 hours, so please only run it if you need to. Your corpus will also differ from the pinned one, so the numbers above won't reproduce exactly. This part also needs the Supabase CLI.

```
.venv/Scripts/python scripts/collect_kb.py
.venv/Scripts/python scripts/parse_kb.py

supabase start -x gotrue,realtime,storage-api,imgproxy,kong,mailpit,postgrest,postgres-meta,studio,edge-runtime,logflare,vector
supabase db reset

$env:DATABASE_URL = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"
.venv/Scripts/python scripts/ingest_kb.py

$env:NEXT_TELEMETRY_DISABLED = "1"
$env:ACCESS_GATE = "off"
$env:RATE_LIMIT = "off"
npm run dev
```

That runs lexical mode. Semantic mode needs a one-time article embed with your own OpenAI key (about $0.01 for the full corpus) through `scripts/embed_kb.py`. See [SPEC.md](SPEC.md) and [AGENTS.md](AGENTS.md) for every command, flag and test.

</details>
