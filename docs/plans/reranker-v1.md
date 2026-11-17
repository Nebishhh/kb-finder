# Reranker v1 (rerank-v1): design

Written 2026-11-16, before any mode was scored on the eval v2 pilot, so
that nothing here is shaped by those results. Design only: nothing is
built, run, tested or paid for. The author did not open `eval/**`,
`cache/**`, the pilot file or the cases file.

This is a plan, not a frozen entry. The configuration and the decision
rule in sections 3 and 6 become binding only when they are copied into a
DECISIONS.md entry marked frozen, before the first real run.

## 1. What must be decided before anything is scored

**Scoring rerank-v1 on the eval v2 pilot needs a later logged decision.**
AGENTS.md "Data Handling" says the pilot text is sent "only to the OpenAI
embeddings API" and "never to a chat model unless a later logged decision
in DECISIONS.md allows it". A reranker sends each query to a chat model.
So:

- Until that decision is logged, no agent and no human runs rerank on the
  pilot cases file, in any form, including a single trial query.
- Building the mode and testing it offline with fakes and made-up queries
  does not need that decision. It needs the approvals in section 8.
- The decision is the owner's. This plan does not assume it will be made.

If the decision is not made, rerank-v1 has no real dataset: eval v1 has
been seen many times and has 21 cases, so a result there would be a first
signal at most, as hybrid's was.

## 2. Goal and hypothesis

**What it is for.** Ordering. Semantic usually has the right article
somewhere in its top 10 but does not always put it first. A reranker reads
the query and the top candidates together and reorders them.

**Hypothesis.** On real student emails (Track A), reordering the semantic
top 10 with a chat model puts the recorded article first more often than
semantic alone, without pushing it out of the top 3 where semantic had it
there.

**Where the hypothesis comes from.** Eval v1 and general evidence listed
in `docs/plans/accuracy-options.md` (section 2). Not from eval v2: no
eval v2 number existed when this was written.

**What it cannot do.** It only reorders the first N results. A target
that semantic ranks below N stays where it is, and a target semantic did
not return stays missing.

## 3. Proposed configuration

To be frozen as written, or changed, before any real run. Nothing below is
tuned on any eval case.

| Item | Proposal | Why |
|---|---|---|
| Mode name | `rerank`, config id `rerank-v1` | Follows `hybrid` / `hybrid-rrf-v1` |
| Gate | Eval-only, `EVAL_MODES_ENABLED` exactly "true"; never a default | AGENTS.md: experimental modes are gated server-side |
| First stage | `semantic` (article-v1), unchanged, depth 50 | It is the production default and the comparator, so the comparison isolates the reranker |
| N | 10 | A judgment about cost and speed against reach. Not tuned |
| Reordered | Ranks 1 to N only. Ranks N+1 to 50 keep the semantic order | Keeps the top 50 set identical to semantic |
| Candidate text | Title plus the public text article-v1 embeds, cut at a fixed character cap per article | Same text semantic already uses. Staff-only text is never sent |
| Candidate labels | Short neutral labels (A, B, C...), mapped back on the server | The model never sees or returns an article ID |
| Candidate order in the prompt | Semantic order | Works against a spurious change, as hybrid's tie-break to semantic does |
| Model | One chat model, named in the frozen entry | See "open choices" below |
| Sampling settings | The most deterministic the API offers, named in the frozen entry | Reduces run-to-run change. It does not remove it |
| Prompt | One fixed text, in a config file, hashed like the embedding config | The hash goes in the frozen entry |
| Output | Structured output: the labels in ranked order, nothing else | No free text is ever displayed or stored |
| Score field | Not a probability. Reordered rows get a rank-derived value, named in the frozen entry | /api/search requires a finite score per row |

**Open choices for the owner, to settle in the frozen entry.**

- **Model.** `accuracy-options.md` suggested `gpt-6.1-terra` on 2026-11-15
  and says prices and model names must be rechecked before building. I
  have not rechecked them.
- **Character cap per article.** Median article length is 128 tokens and
  p95 is 408 (DECISIONS.md), so a cap near the p95 sends most articles
  whole. The exact number is a free choice.
- **Prompt wording.** Written and tested only on made-up queries.

## 4. Request flow and failure rules

One search in `rerank` mode:

1. Embed the query and run `kb_search_semantic` at depth 50, as semantic
   does. One query embedding.
2. Fetch the text of the top N articles.
3. One chat-model call with the query and the N candidates.
4. Validate the output: it must be exactly a permutation of the N labels.
5. Return the reordered N, then ranks N+1 to 50 unchanged.

**Fail closed, no fallback (D1).** If the semantic leg fails, the model
call fails or times out, or the output is not a permutation, the request
returns the standard search failure. The server never returns the
semantic order in place of a reranked one, and never retries.

**Fewer than N results.** Rerank what there is. Zero or one result needs
no model call.

**Limits.** One search is one count against the daily cap, as now.

**Ranking code outside SQL.** The model's order is ranking outside SQL.
AGENTS.md says the hybrid fusion "is the only ranking code outside SQL;
do not add another". This design needs that rule changed, as a logged
exception like hybrid's. The server code itself does no relevance
judgment: it validates and applies the permutation.

## 5. Privacy and keys

- **New recipient of query text.** The query goes to a chat endpoint, not
  only the embeddings endpoint. Public article text goes with it. AGENTS.md
  "Data Handling" lists the modes that send query text to OpenAI and would
  need `rerank` added.
- **No storage at OpenAI beyond the default abuse logs.** Application
  state storage must be switched off on every call (`store` off, per
  `accuracy-options.md`; recheck the parameter at build time).
- **Nothing query-derived is stored here.** No cache of rerank outputs:
  an output keyed by a query is query-derived. No prompt, response, query
  text, hash or vector in logs, disk or the database. Errors stay fixed
  strings plus an error class.
- **Keys.** The hosted key is embeddings-only with a one-model allow-list
  (DECISIONS.md), and this plan does not change it. Evals use the local
  eval key in the owner's shell, which would need chat access to the one
  chosen model. Only the owner sets keys.
- **Hosted app.** Unaffected. The mode is gated off there, and the hosted
  key could not call a chat model if it were on.
- **Prompt injection.** Article text and query text are data inside the
  prompt. The only thing the output can change is the order of N known
  articles, because anything that is not a permutation fails the request.

## 6. Decision rule (to freeze before scoring)

**Dataset.** The eval v2 pilot cases file (36 cases, Track A only, sha256
in the 2026-11-16 decision-rules entry), and only after the logged
decision in section 1. By then lexical, semantic and hybrid will have been
scored on it. The rule below is fixed before those scores exist; it must
be frozen in DECISIONS.md without edits made in light of them. Any edit
after eval v2 is scored is logged as made after seeing those results.

**Comparator.** That session's semantic run, same commit, same cases
file. Order: semantic, then rerank, each with `--json-out`.

**Primary: Track A Top-1.** Paired, per case. A win is a case where
rerank has the target at rank 1 and semantic does not; a loss is the
reverse. Top-1 is the primary because the reranker's claim is about what
comes first. This differs from the eval v2 rule for hybrid and lexical,
whose primary is Top-3; it is a choice for the owner to confirm.

**Bar.** The eval v2 bar: more wins than losses and an exact two-sided
sign test p < 0.05. In wins-losses: 6-0, 8-1, 10-2, 12-3, 13-4, 15-5, or
better. The bar depends on the number of disagreements, not on 36.
Anything short of it is not a result. A net change of 1 to 3 cases is
noise.

**Secondary, reported as secondary:** Track A Top-3, same bar. MRR@50 is
reported only.

**Two runs, because the model is not repeatable.** Rerank is run twice in
the session. The first complete run is the scored run. The result is real
only if the bar is met in the first run and also in the second. The number
of cases whose Top-1 hit differs between the two rerank runs is reported.

**Headroom check, fixed in advance.** Rerank can win on Top-1 only where
semantic has the target at ranks 2 to 10, and can lose only where semantic
has it at rank 1. That count is read from the comparator report before the
rerank run. If fewer than 6 cases sit at ranks 2 to 10, the bar cannot be
met: the outcome is logged as "not testable on this pilot" and rerank is
not run on it. This is a rule about the dataset, written before the count
is known.

**Regression.** Rerank regresses if Top-1 or Top-3 is a real loss under
the bar, or Top-3 has a net loss of 4 or more. A target cannot leave the
top 50, because the set is unchanged; if one does, that is a bug and the
run is void.

**Aborted runs.** A failed request stops run_eval.py with no metrics. An
aborted run is logged and restarted from the first case. After three
aborted attempts the outcome is "not testable as configured".

**Tooling.** `scripts/paired_eval.py` would need a third rule (primary
Track A Top-1, the eval v2 bar). That is a small, separate change.

## 7. What each outcome can and cannot change

No outcome changes production, the default mode, or any frozen
configuration.

- **Real win in both runs, no regression:** rerank-v1 becomes a candidate
  for a promotion review. That review is its own logged decision and needs
  measured latency, a cost ceiling at the daily cap, the hosted key
  change, and a view on whether 36 email-only cases are enough.
- **Not a result:** it stays built and gated. It is not rescored on the
  pilot with a different model, N or prompt; any such change is a new
  config id and a new logged experiment, and is not a blind test on these
  cases.
- **Regression:** this configuration stops.
- **Not testable:** logged as such. It waits for a larger eval set.

**What it cannot show in any case.** Nothing about technician wording
(no Track B). Nothing about abstention (no negatives). The label is one
recorded article, so reordering toward a different, equally good article
scores as a loss.

## 8. Changes this would need, each with its own approval

Nothing here is requested now.

| Change | Why |
|---|---|
| DECISIONS.md: logged decision allowing pilot text to a chat model | AGENTS.md "Data Handling"; without it, no pilot scoring |
| DECISIONS.md: frozen rerank-v1 configuration and rule | Sections 3 and 6 |
| AGENTS.md "Retrieval Rules" | "Do not add ... reranking until that experiment begins"; the ranking-outside-SQL exception |
| AGENTS.md "Data Handling", "Paid Calls and Keys", "Current Stack" | A new mode that sends query text to a chat endpoint; paid chat calls are human-only |
| SPEC.md | Reranking is listed as out of scope; the mode list and /api/search |
| Search mode added | CLAUDE.md approval checkpoint: adding a search mode |
| `config/` file for the prompt and settings | A new frozen config with a hash |
| `package.json` | Only if the chat call needs a dependency the embeddings call does not. Not checked |
| Migration | Not expected: kb_app can already select from articles (D10). To confirm at build |

## 9. Build and test outline (after approvals)

1. Config file and loader with a hash, as for the embedding config.
2. The rerank step as a pure function over (query, candidates, model
   client), tested offline with a fake client: valid permutation, missing
   label, duplicate label, extra label, free text, timeout, fewer than N.
3. The gated mode in /api/search, with the fail-closed rules of section 4.
4. Sentinel tests: the query and the prompt never reach a log or an error.
5. `paired_eval.py` rule and tests.
6. A local smoke run with `tests/fixtures/fake_cases.json` only. Agents
   never make a paid chat call; the owner runs anything real.

## 10. What I could not verify

- Current model names, prices, structured-output support and the exact
  parameter that disables storage. All from `accuracy-options.md`, dated
  2026-11-15, not rechecked.
- Latency. No figure exists; one extra model call in sequence is the only
  known fact.
- Cost. `accuracy-options.md` estimates about 3,000 input tokens per
  search at 10 candidates. Two rerank runs of 36 cases would be about 72
  calls. Both figures are estimates.
- Whether fetching candidate text needs a new SQL function.
