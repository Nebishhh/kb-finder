# Eval v2 and abstention plan

Written 2026-11-14 as a planning reply. Where the body conflicts with the 'Owner decisions' section at the end, the owner decisions win. Line citations refer to the repo as of commit 84dee97 and may drift.

One disclosure first: besides the files you listed, I ran one search over `scripts/run_eval.py` to see what its `--json-out` report contains. I did not open `eval/**`.

## Your numbers (confirmed)

| Claim | Where |
|---|---|
| Semantic Top-3 is 16/21 on both tracks | `DECISIONS.md:321-324`, `README.md:30`, `README.md:94-95` |
| Technician Top-1 is 6/21 (down from 8 lexical) | `DECISIONS.md:323`, `:334`, `README.md:95` |
| No abstention | `README.md:43`, `:97`, `:128`; `DECISIONS.md:73-75`; `SPEC.md:456-458` |
| Small set: 21 positives, 4 negatives | `DECISIONS.md:111-112`, `README.md:44`, `:102` |

No mismatch with what you believe. Three things to keep in view:

- **The two 16s are different cases.** Track A misses include 006, 014 and 012; Track B misses include 004, 012 and 021 (`DECISIONS.md:332-338`). Two more misses per track are not named anywhere I may read.
- **`DECISIONS.md:102` still says "20 positives and 5 negatives".** It is the plan, superseded by `:111-112`.
- **`SPEC.md:483-485` lists "no-match thresholds" as out of scope before AfroTech.** Confirm that gate has passed before starting item 2.

## 1. Why queries fail

**Reported in the docs**
- **Short technician wording that matches a title favors keywords.** Cases 001, 008, 010 and 016 dropped from rank 1 to 2 or 3 on Track B (`DECISIONS.md:334-336`, `README.md:104`).
- **The modes see different fields.** Semantic embeds title and full text only, with no tags or description (`DECISIONS.md:242-245`, `:340-342`).
- **Some targets are partial matches.** No article names the exact symptom for 009 and 018, and 005 was a judgment call (`DECISIONS.md:112-114`, `:122-123`).
- **Ranking is unstable to wording.** Case 012 went from rank 29 to 5 on Track A and from 6 to 39 on Track B (`DECISIONS.md:337-338`).

**Hypotheses (not confirmed by anything I can read)**
- **Strict labels count helpful articles as misses.** Adjacent and partial articles are deliberately not targets (`DECISIONS.md:125-131`), and the lexical run noted two negatives returning articles "close to what the student asked for" (`:220-224`).
- **Near-duplicates crowd the top 3.** The KB has per-OS copies of one how-to (`DECISIONS.md:69-71`).
- **The answer sits in staff-only text.** It is not searchable in v1 (`DECISIONS.md:141-142`), 1,255 articles have it (`:672`), and 013 became a negative for that reason (`:107-109`). article-v2 is unscored (`:675-679`).
- **One vector per whole article dilutes long ones.** Probably a small effect: median length is 128 tokens (`DECISIONS.md:249-251`).
- **Raw queries carry noise.** They are embedded as typed (`DECISIONS.md:245`).

I cannot attribute the unnamed misses to any of these.

## 2. Abstention experiment

**Confirmed constraints**
- The score is cosine similarity, not a probability (`DECISIONS.md:256-257`).
- **A plain top-1 cutoff has already been observed to fail:** negatives scored 0.33 to 0.62, "overlapping real matches" (`DECISIONS.md:338-340`).
- Two of the four negatives (013, 025) are debatable labels (`DECISIONS.md:220-224`).
- The eval report holds case IDs, ranks, scores and article IDs, and no query text (`scripts/run_eval.py:31-33`).

**Proposal**

Signals, all computed from the returned list after search, with no frozen config touched:
1. Top-1 score (expected to fail; measured as the baseline).
2. Top-1 minus top-2.
3. Top-1 minus the mean of ranks 2 to 10.
4. Whether the semantic top-1 also appears in the lexical top 10. This overlaps the hybrid session, so drop it if you want the two kept apart.

Two questions, reported separately:
- **Coverage:** negative vs positive. Only 8 negative queries (4 cases × 2 tracks).
- **Confidence:** is a target in the top 3? This has 42 queries and about 10 misses, so it is the more informative one, and it supports a "not sure" banner rather than "the KB doesn't cover this".

Procedure for you to run:
1. Check whether you kept a `--json-out` report from the 2026-10-01 scored run. If so, use it and skip step 2.
2. Otherwise start the app as for an eval and run the existing command, writing outside the repo to a folder that already exists:

```bash
.venv/Scripts/python scripts/run_eval.py --mode semantic --cases eval/cases.json --json-out ..\kb-finder-private\semantic-abstention.json
```

3. Run an analysis script over that report. **This script does not exist.** I would write it and test it only on a report made from `tests/fixtures/fake_cases.json`; you would run it on the real one, as with `compare_eval_json.py` (`AGENTS.md:50-51`).

A rerun is for this analysis only. Query embeddings are not cached, so it can differ slightly and does not replace the scored run (`SPEC.md:475-479`).

**What this cannot show.** A threshold tuned on 4 negatives cannot be validated on those 4 negatives. Their scores have also already been seen, and two of them are borderline. Treat eval v1 as exploratory: it can rule a signal out, not approve one. Write the operating point (for example "answer at least X of positives, abstain on at least Y of negatives") before any held-out run; I am not proposing the numbers.

**Held-out negatives**
- **Best:** real tickets resolved without a KB article, from the eval v2 sample in item 3, after a labeler confirms by category browsing that no article fits (the rule at `DECISIONS.md:64-67`).
- **Also:** tickets referred outside IT.
- **Proxy only:** take a positive case, delete its target IDs from the returned list, and treat it as "the KB no longer covers this". Near-duplicates and adjacent articles stay in the list, so this is not a realistic negative.

## 3. Eval v2 from real tickets

**A rule conflict to settle first.** `AGENTS.md` Data Handling says "Do not store names, IDs, emails, phone numbers, or ticket data" and that ticket text stays ephemeral. A stored eval set derived from tickets needs an explicit policy change from you as well as supervisor approval. That approval should also say whether rewritten queries may be sent to OpenAI, which semantic mode does (`AGENTS.md:110-112`). I can't advise on university or legal policy; that is the supervisor's call.

**Proposal**

- **Sampling**
  - Fix the frame first: a date window and the ticket categories the Technology KB covers.
  - Exclude sensitive categories (security incidents, conduct, anything medical or accommodation-related).
  - Draw ticket numbers with a seeded random generator, inside the ticket system, by someone authorized.
  - Oversample, and fix the exclusion rules in advance (unintelligible, duplicate, not IT, cannot be de-identified).
  - Publish only the seed and the counts.
- **Size.** For a rate near 75%, the 95% margin is about ±12 points at 50 cases, ±8.5 at 100 and ±6 at 200. `README.md:134` currently plans about 50.
- **Rewriting**
  - Done by an authorized person, not by me and not by a model.
  - Remove names, IDs, emails, phone numbers, serials, asset tags, hostnames, IP addresses, rooms and course sections.
  - Keep length, word choice, order and product names such as Duo or D2L.
  - A second person checks each rewrite. Drop any ticket that stays recognisable.
  - Freeze the queries before labeling, as in v1 (`DECISIONS.md:77-79`).
- **Tracks.** The schema needs both fields (`SPEC.md:407-415`): the user's description for `customer_query`, the technician's short summary for `technician_query`.
- **Labels**
  - The target is the article the technician actually used, checked to be one of the 3,109.
  - Keep the v1 target rule: an article counts only if following it alone resolves the issue (`DECISIONS.md:125-131`).
  - Known bias: technicians find articles with today's keyword search, so these labels lean toward lexical.
- **Several valid articles.** List each one that resolves the issue on its own; any one is a hit (`DECISIONS.md:69-71`).
- **No article used.** A labeler browses categories. If nothing fits, it is a negative. If something fits, it is a positive, flagged as not technician-linked. If the fix is in a non-public article, report it separately.
- **Disagreement.** Two labelers work independently and blind to system output; a third person or the supervisor decides ties; report the agreement rate; no label changes after the first run (`DECISIONS.md:99`).
- **Where it lives**
  - Supervisor-approved storage outside this repo. `run_eval.py` takes any `--cases` path (`SPEC.md:442-443`), so no code change is needed.
  - The repo gets the file's sha256, counts and aggregate metrics only. No per-case descriptions in `DECISIONS.md`, unlike `:107-116`.
  - Agents never read it, and it never goes into HANDOFF.md, memory or artifacts.
  - Agree a retention and deletion date.
- **Thresholds before any run**
  - Commit the primary metric, the comparison and the pass bars to `DECISIONS.md` before the first run.
  - I suggest a paired comparison on the cases where two modes disagree, plus a minimum lower bound on Top-3.
  - The numbers are yours to set.

## 4. What is too small to be real on 21 cases

- **One case is 4.8 points** (`README.md:102`).
- **A single score is wide.** The 95% interval for 16/21 is roughly 55% to 89%, about 12 to 19 cases.
- **A paired change needs about 6 wins with no losses, or 8 wins against 1,** to pass a two-sided sign test at 5%.
- **The existing headline does not meet that bar.** Semantic over lexical was 5 gained and 2 lost on Track A and 6 gained and 1 lost on Track B (`DECISIONS.md:332-334`); by my calculation those are p ≈ 0.45 and p ≈ 0.13.
- **The Top-1 drop from 8 to 6 is two cases,** which is inside the noise, even though the four named cases give a plausible mechanism.

In practice, treat any net change of 1 to 3 cases on a track as noise, and note that `DECISIONS.md:214-216` already takes that position for one or two. Semantic reruns can also shift slightly on their own (`README.md:130`).

## Owner decisions, 2026-11-14
- AfroTech is done. The SPEC.md "no-match thresholds out of scope" line is outdated and should be updated when abstention work starts.
- Supervisor approval is not required for eval v2; the owner decided this.
- The owner selects, rewrites and labels tickets by hand. No AI agent reads raw tickets or browses TeamDynamix. AI tools only handle rows after de-identification (formatting, duplicate checks, flagging anything that still looks identifying).
- A TeamDynamix report or export may be used to draw the random sample. Any raw export stays on the owner's work machine, never in this repo or a personal cloud folder, and is deleted after the cleaned cases are written.
- Start with a 20-30 case pilot, then scale to 100-150 cases plus negatives.
- A technician-linked article is a candidate label; the owner verifies it resolves the issue before it counts.
- AGENTS.md Data Handling will be updated, when collection starts, to allow rewritten, de-identified test cases derived from tickets, stored only outside this repo.
- Hybrid retrieval is the next experiment. Its first result on eval v1 needs at least 6 wins and 0 losses, or 8 wins and at most 1 loss, against frozen semantic on a track to count as real.
- Labeling is done by the owner alone, so a second labeler and an agreement rate are not available. This is a known limitation of eval v2, recorded rather than worked around.
