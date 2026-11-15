# Accuracy options compared

Researched 2026-11-15. Prices and model names were current on that date; recheck before building.

Nothing was edited, built, tested or paid for. I did not open `eval/**` or `cache/**`.

**The main conclusion:** your measured weakness is ordering, not finding, and only one idea on the list is aimed squarely at ordering: reranking. Everything else mostly helps find articles, and semantic already puts a target in the top 50 for all 21 cases on both tracks (`DECISIONS.md:321-324`).

## How to read the labels

- **Confirmed** means I read it in this repo or on an OpenAI page today (2026-11-15).
- **General evidence** means a published benchmark on other data. It is not proof for this KB.
- **Estimate** means my arithmetic or judgment. No latency number below is measured.

## Sourced figures used throughout

| Item | Figure | Source |
|---|---|---|
| text-embedding-3-small | $0.02 per 1M tokens | OpenAI pricing page |
| text-embedding-3-large | $0.13 per 1M tokens | OpenAI pricing page |
| `gpt-6-luna` | $0.10 in, $0.50 out per 1M | OpenAI pricing page |
| `gpt-6.1-terra` | $1.00 in, $5.00 out | OpenAI pricing page |
| `gpt-6.1-sol` | $2.00 in, $10.00 out | OpenAI pricing page |
| `gpt-6-astra` | $10.00 in, $50.00 out | OpenAI pricing page |
| Newer OpenAI embedding model | None listed; the guide names only the two `-3` models | OpenAI embeddings guide |
| MTEB score | 62.3% small, 64.6% large | OpenAI embeddings guide |
| Corpus size | 544,549 tokens (article-v1), 569,605 sent (article-v2) | `DECISIONS.md:276`, `:696` |
| Article length | median 128 tokens, p95 408, one article truncated | `DECISIONS.md:249-252` |

The pricing page carries no "last updated" date. Batch prices for embeddings are not on it.

## The eight ideas

### 1. Hybrid RRF (built, baseline)
- **Targets:** ordering, by letting keyword evidence move a title match up. General evidence: the 2009 RRF paper found fusion beat its individual rankers.
- **Price:** nothing one-time; per search the same single embedding as semantic.
- **Speed:** estimate, about the same as semantic, since the keyword query runs during the embedding call.
- **Infrastructure:** none new. Two pool connections per search instead of one.
- **Privacy:** nothing new.
- **Effort and test:** done. Scored by the frozen rule in the hybrid-rrf-v1 entry.
- **Combines with:** everything. It is a natural first stage for a reranker.

### 2. Reranking the top N with an OpenAI chat model
- **Targets:** ordering, directly. Ten of your Track B cases have the target in the top 3 but not first, which is what a reranker reorders.
- **General evidence:** the RankGPT paper reports GPT-4 reranking beat a fine-tuned reranker by 2.3 nDCG on average across eight BEIR sets. A separate vendor post (Anthropic) reports top-20 retrieval failures falling from 2.9% to 1.9% when a reranker was added to hybrid search.
- **Price:** nothing one-time. Per search, estimated for 10 candidates at about 3,000 input tokens: about $0.0003 on luna, $0.003 on terra, $0.006 on sol, $0.03 on astra. At the 2,000-a-day cap that is a ceiling of about $0.60, $6, $12 or $65 a day. Reasoning tokens would add to this.
- **Speed:** one more model call in sequence, after retrieval. My estimate is around a second or more; unverified. OpenAI's latency guide says each sequential call "adds another wait."
- **Infrastructure:**
  - The hosted key is embeddings-only with a one-model allow-list (`DECISIONS.md:444-451`). It needs a new permission and model, or a second key.
  - No migration. One extra query to fetch candidate text.
  - New failure modes: timeouts, malformed output, and a model returning an ID that was not a candidate. Fail closed: validate the output is a permutation of the candidates, else 500 with no fallback (D1).
  - Results are not repeatable run to run, which weakens paired comparisons.
- **Privacy:** query text goes to a second OpenAI endpoint, and public article text goes with it for the first time at query time. The Responses API keeps application state for 30 days by default, so `store` must be off. Abuse logs are 30 days, the same as embeddings.
- **Effort and test:** medium. New gated mode; offline tests with a fake model. Test it over the frozen semantic top N so the comparison isolates the reranker.
- **Combines with:** stacks on any first stage. Competes with normalization for the latency budget.
- **Vendor note:** a dedicated reranker model (Cohere, Voyage) is built for this job and is typically faster and deterministic. Staying with OpenAI gives up that; it keeps one key, one balance and one recipient of query text.

### 3. text-embedding-3-large
- **Targets:** mostly finding; may also reorder. General evidence: MTEB 64.6% against 62.3%, a modest gap on a broad benchmark.
- **Price:** estimate, about $0.07 to re-embed the corpus once. Per search, a fraction of a cent of a cent.
- **Speed:** estimate, no meaningful change.
- **Infrastructure:**
  - At 1,536 dimensions, no migration: the table and function require exactly 1,536 (`supabase/migrations/20261001011735_add_semantic_embeddings.sql:14`, `:33`), and OpenAI's `dimensions` parameter shortens large vectors. The guide gives no quality figure at 1,536.
  - At the native 3,072, a migration and a new function.
  - Either way: a new file under `config/embedding/`, a paid embed you run, a load to hosted, and the key's model allow-list.
- **Privacy:** nothing new.
- **Effort and test:** small. A new eval-only mode, paired against semantic.
- **Combines with:** hybrid, reranking, article-v2 (which would need its own re-embed).
- **Vendor note:** third-party guides report Voyage and Google embedding models ahead of OpenAI's; those are vendor-reported claims I did not verify.

### 4. Article-v2 (staff-only text)
- **Targets:** finding, for the cases where the fix sits in staff-only text. No benchmark applies; it is a property of this KB.
- **Price:** already paid (about $0.011, `DECISIONS.md:697`). A hybrid over the v2 pair costs nothing more.
- **Speed:** no change.
- **Infrastructure:** built locally. Promotion needs the migration pushed and the v2 embeddings loaded on hosted. A `hybrid-v2` mode needs no fusion change.
- **Privacy:** nothing new at query time.
- **Effort and test:** small. Your rule already says it is scored on eval v2 only (`DECISIONS.md:675-679`).
- **Combines with:** everything.

### 5. Document-side expansion
- **Targets:** finding, by closing the vocabulary gap from the article side.
- **General evidence:** doc2query raised keyword-search MRR@10 on MS MARCO from 0.184 to 0.218, and its T5 successor to 0.277. That evidence is for keyword search; I have no sourced figure for the effect on embeddings.
- **Price:** estimate, one-time about 1.0M input and 0.5M output tokens: roughly $0.35 on luna, $3.50 on terra, $7 on sol, $33 on astra, plus about $0.02 to re-embed. Nothing per search.
- **Speed:** no change per search.
- **Infrastructure:** an offline, human-run paid script like the embed. A migration for the new text and its staleness check. No hosted key change, since generation is offline.
- **Privacy:** no query text anywhere new. Public article text goes to a chat model once.
- **Effort and test:** large. The prompt, the model and the number of phrasings are all free choices that must be fixed blind. Generated text must never be displayed.
- **Combines with:** everything. Overlaps normalization: both attack wording mismatch.

### 6. Query normalization (Phase 6)
- **Targets:** finding, on messy student wording (Track A). Your measured weakness is terse technician wording on Track B, which has little to normalize.
- **General evidence:** none fetched today.
- **Price:** estimate, under $0.001 per search on terra, far less on luna.
- **Speed:** one more model call in sequence, before retrieval.
- **Infrastructure:** the same key change as reranking. New failure mode: a rewrite that drops the detail that mattered.
- **Privacy:** query text to a second OpenAI endpoint.
- **Effort and test:** medium.
- **Combines with:** stackable, but two model calls per search if reranking is also on.

### 7. Chunking
- **Targets:** finding inside long articles.
- **Why it is weak here:** the median article is 128 tokens, p95 is 408, and one article is truncated. `SPEC.md` already defers it unless an eval shows long articles are missed.
- **Price:** estimate, about $0.01 to $0.02 to re-embed.
- **Infrastructure:** large: a new table shape, a new function and a migration.
- **Effort:** large, for little expected gain.

### 8. Abstention
- **Targets:** usability, not accuracy.
- **Confirmed:** a plain score cutoff fails; negatives scored 0.33 to 0.62, overlapping real matches (`DECISIONS.md:338-340`).
- **Price and speed:** nothing if computed from the returned list. If a reranker exists, it can also be asked whether any candidate fits, at no extra call.
- **Infrastructure:** a response field and a UI line.
- **Test:** needs held-out negatives from eval v2; four negatives cannot validate a threshold.
- **Combines with:** everything, and best with reranking.

## Comparison table

| Idea | Targets | One-time | Per search | Added latency | Hosted key change | Migration | Query text somewhere new | Effort |
|---|---|---|---|---|---|---|---|---|
| 1. Hybrid | Ordering | $0 | Same as semantic | About none | No | No | No | Done |
| 2. Rerank | Ordering | $0 | $0.0003 to $0.03 | One model call | Yes | No | Yes, chat endpoint | Medium |
| 3. 3-large | Finding | About $0.07 | Negligible | About none | Allow-list | No at 1,536 | No | Small |
| 4. Article-v2 | Finding | Paid | None | None | No | Push to hosted | No | Small |
| 5. Expansion | Finding | $0.35 to $33 | None | None | No | Yes | No | Large |
| 6. Normalize | Finding (Track A) | $0 | Under $0.001 | One model call | Yes | No | Yes, chat endpoint | Medium |
| 7. Chunking | Finding (long) | About $0.02 | None | Small | No | Yes | No | Large |
| 8. Abstention | Usability | $0 | $0 | None | No | No | No | Small to medium |

All dollar and latency cells are estimates built on the sourced prices above.

## Recommended order

1. **Score hybrid** as already frozen. It is built and free.
2. **Eval v2 pilot before anything new is scored.** Eval v1 has been seen, and each further experiment on 21 cases adds another chance of a false signal. This is the step that makes every later result mean something.
3. **Reranking.** It is the only idea aimed at the measured weakness, it has the strongest general evidence, and cost is not your constraint. Fix the model, N and prompt before any run; my suggestion is `gpt-6.1-terra` over the semantic top 10, which is a judgment about speed against quality, not something a source told me.
4. **Article-v2 and hybrid-v2 on eval v2.** Already built, so nearly free to score.
5. **text-embedding-3-large at 1,536 dimensions.** Cheap and small, but the general evidence is modest.
6. **Document-side expansion**, only if finding is still a problem on eval v2.
7. **Normalization**, last: it targets the track that is not your weakness and costs a model call per search.

Abstention runs alongside from step 2, since it needs the eval v2 negatives.

## What to design when

- **Now:** the reranking experiment (pre-registered config and rule, key handling, fail-closed output validation, `store` off), and the abstention signals.
- **Later:** 3-large, document expansion, normalization. Design each when its turn comes, so the choices are not made with stale model options.
- **Not at all, for now:** chunking.

## What I could not verify

- OpenAI's embedding announcement page returned 403, so I have no quality figure for 3-large shortened to 1,536.
- Whether `gpt-6-luna` supports structured outputs is not stated on the pages I read.
- No latency figure here is measured; measure locally with made-up queries before trusting any of them.

Sources:
- [OpenAI API pricing](https://developers.openai.com/api/docs/pricing)
- [OpenAI embeddings guide](https://developers.openai.com/api/docs/guides/embeddings)
- [OpenAI models](https://developers.openai.com/api/docs/models)
- [OpenAI latency optimization guide](https://developers.openai.com/api/docs/guides/latency-optimization)
- [OpenAI structured outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs)
- [OpenAI data controls ("your data")](https://developers.openai.com/api/docs/guides/your-data)
- [Is ChatGPT Good at Search? (RankGPT)](https://arxiv.org/html/2304.09542v3)
- [Document Expansion by Query Prediction (doc2query)](https://arxiv.org/pdf/1904.08375)
- [Pretrained Transformers for Text Ranking](https://arxiv.org/pdf/2010.06467)
- [Anthropic: contextual retrieval](https://www.anthropic.com/engineering/contextual-retrieval?utm_source=tldrai)
- [Cormack et al., Reciprocal Rank Fusion (PDF)](https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf)
- [OpenRouter: best embedding models 2026](https://openrouter.ai/blog/insights/best-embedding-models-2026/)
