# Zotistics evidence and Phase 1 recommendation ranking

Confirmed with the user on 2026-10-12. This increment adds verified historical
grade evidence and charts, plus an opt-in deterministic ranker. Dynamic LLM
weights are outside Phase 1.

## Existing pipeline and integration

The agent selects tools and proposes course IDs, priorities and reasons. The
backend retrieves catalog/section data, checks enrollment conditions, adds
prerequisite and schedule results, then sends recommendation cards through SSE
and saves them for session replay. The old course search returns the first 30
catalog entries in course-number order; it is not a recommendation score.

`db.retrieve_course_candidates` shares the old filters without a display limit.
`app.data.recommendations.rank_courses` expands the full matching catalog pool
into current primary-section candidates, reads local RMP and Zotistics query
caches, scores the pool, deduplicates by course while retaining the best primary
section, then selects Top K. It never searches historical grades to infer who
teaches the target term. Multi-instructor sections have no invented joint rating.

`RECOMMENDATION_RANKING_ENABLED=false` is the default. Legacy candidate/card
ordering remains in effect until this flag is explicitly enabled. When enabled,
the search tool uses the full-pool ranker and proposed cards/primary sections use
the same scoring core. LLM selection and explanations remain part of the chat
workflow; this phase does not guarantee that the LLM chooses the global Top K.

The ranker has no I/O or LLM dependency. Its input is explicit numerical features;
eligibility, degree/GE requirements, prerequisites and conflicts stay with the
existing retrieval/validation components. Unknown validation states stay unknown.

## Historical grade source

The original [Zotistics](https://zotistics.com/) form supports exact instructor,
department and course-number combinations. Its Details rows are checked against
the requested scope before counts are accepted. Source scripts are parsed as
bounded data literals, never executed.

Recommendations collect grades for current primary lecturers before constructing
cards; repeated card construction reuses request evidence. Professor evaluations
can attach the same evidence to tool results and to standalone charts. Plain
offering/cross-term instructor lookups do not automatically fetch grades.

An exact professor-course `no_data` result permits a **professor-wide, all
departments** fallback. Both the source scope and requested course are retained.
Timeout, parse failure, ambiguous name and unknown name do not permit this
fallback. A source-verified full-name/canonical-name relation is reused; matching
only a surname and first initial does not verify a conflicting full name.

The adapter exposes `found`, `status`, `scope`, `query`, `resolved_instructor_name`,
`source_url`, `coverage`, `grades`, and limitations. Counts and source percentages
are validated together. `get_cached_query_result` reads valid, unexpired positive
or confirmed-empty evidence without network/writes; `get_cached_grade_distribution`
is positive-only. Successful records have a seven-day TTL; confirmed empty
responses have a one-hour TTL. Failures are not cached as confirmed absence.
Runtime cache files live under ignored `data/runtime/zotistics/`.

Website percentages include A/B/C/D/F/P/NP in their denominator. Ranking uses:

```text
A rate = A / (A + B + C + D + F)
```

`graded` is the A–F count; `samples` additionally includes P/NP. They are grade
records, not unique students across all terms. Raw bucket counts do not distinguish
+/−. The source-reported GPA is stored separately from the bucket-based GPA
estimate; no claim is made that the methods are equivalent. Neither GPA is a
ranking feature in this phase. Do not combine sample counts with Anteater's
potentially overlapping aggregates.

The site currently advertises Spring 2023 as its latest grade update. Coverage
labels use actual returned Details rows, not a fabricated continuous date range.
[Official source limitations](https://zotistics.com/info) include omitted small or
incomplete classes and uncertain/missing co-instructor attribution. Historical
outcomes do not measure teaching quality, workload, or a student's future A odds.

## Scoring contract

`rank_candidates(candidates, weights=None, reliability=None, top_k=None)` preserves
each candidate's identifiers/section metadata and adds a `ranking` block. Feature
input is `features[name] = {value, sample_count, ...source metadata}`:

| Feature | Input | Normalized score | Default weight | Default k |
| --- | --- | --- | ---: | ---: |
| `grade_a_rate` | A–F proportion, 0–1 | `value * 100` | 0.6 | 100 |
| `professor_rating` | RMP score, 1–5 | `(value - 1) * 25` | 0.4 | 10 |

`build_candidate_features` prefers exact A/graded counts over rounded percentages.
Existing `pct_A` is 0–100 and is divided by 100 once. Professor ratings describe
the professor overall, not their rating for one specific course. Pair/fallback
grading requires the supplied person's membership in a primary lecture/seminar;
TA/discussion evidence does not become a lecturer feature. Full-name RMP results
also require the returned full name to agree with the requested full name.

For each usable feature:

```text
factor = n / (n + k)
adjusted = 50 + factor * (normalized - 50)
final_score = sum(adjusted * effective_weight)
```

50 is a neutral scoring baseline, not an observed UCI A rate.
The fixed baseline is 50 in Phase 1; weights and k values are configurable. They
are provisional engineering defaults, not empirically optimized parameters or
statistical confidence intervals. A 90% A rate with n=10 yields 53.64 ranking
points; with n=500 it yields 83.33. The chart still shows the original 90%.

Missing/nonfinite/out-of-range values and absent/nonpositive sample counts have no
adjusted value. Missing is not zero; a real A=0 with positive graded count is valid.
Weights are finite, nonnegative supported-feature values, with positive total,
and are normalized. A missing feature redistributes weight among usable features;
all enabled features missing yields `final_score=None` and an unranked position
after scored candidates. Equal scores preserve input order. The score describes
available evidence: different coverage can produce different effective weights,
so it is not a universal course-quality measure.

Returned explanations include configured/effective weights, raw/normalized/
adjusted feature values, sample counts, reliability factors, contributions,
missing/uncertain reasons and final score. Instructor-wide fallback remains
explicitly uncertain and is never relabeled as a course-specific distribution.

## Configuration and example

To enable the experimental backend ordering, set the flag in the server's
environment. Optional JSON objects override fixed weights and k values:

```dotenv
RECOMMENDATION_RANKING_ENABLED=true
RECOMMENDATION_RANKING_WEIGHTS={"grade_a_rate":0.6,"professor_rating":0.4}
RECOMMENDATION_RANKING_RELIABILITY={"grade_a_rate":100,"professor_rating":10}
```

Full-pool ranking reads source snapshots/cache only, with memoized course-person
and professor lookups. It does not make live requests for every catalog entry.
Cache misses are returned as missing features with coverage counts; no silent
course-number cutoff is applied before scoring. Caller-supplied lookup functions
can supply other already-verified snapshots programmatically.

The following synthetic example demonstrates changing weights, without API calls:

```python
from app.recommendations.ranking import rank_candidates

candidates = [
    {"course_id": "Example A", "features": {
        "grade_a_rate": {"value": .90, "sample_count": 500},
        "professor_rating": {"value": 3.0, "sample_count": 50}}},
    {"course_id": "Example B", "features": {
        "grade_a_rate": {"value": .60, "sample_count": 500},
        "professor_rating": {"value": 4.5, "sample_count": 50}}},
]
default = rank_candidates(candidates)
teaching_weighted = rank_candidates(candidates, weights={
    "grade_a_rate": .3, "professor_rating": .7})
```

Default weights rank A (70.00) before B (67.50). The second configuration ranks B
(74.38) before A (60.00). These are synthetic decision scores, not real courses.

## Charts and verification

Course/primary-professor history in recommendation cards uses collapsed native
details controls. Standalone professor comparison charts are open, side by side
on desktop and stacked on narrow screens. Their bars use original counts divided
by total source records; ranking A/(A–F) is labeled separately. P/NP remain visible;
W is rendered only when the source supplies it. GPA, samples, actual coverage and
source link remain visible. Invalid/unavailable data does not produce fake zero
bars. `grade_charts` is saved with the assistant turn and restored through the
same renderer used by the live SSE response. Charts are produced from structured
evidence, not model-authored HTML; source labels are escaped.

Offline regression commands:

```sh
.venv/bin/python -m pytest tests/test_zotistics_data.py \
  tests/test_agent_zotistics.py tests/test_grade_evidence_pipeline.py \
  tests/test_recommendation_ranking.py tests/test_recommendation_ranking_integration.py \
  tests/test_zotistics_cards.py --no-cov -q
```

Tests cover scoring, normalization, weights, sample shrinkage, missing values,
stable ties, full-pool TopK beyond position 30, read-only caching, source/name/role
scope, fallback replay, SSE/session persistence and raw chart denominators. The
optional `SOLON_BROWSER_CHECK=1` frontend test verifies real Chromium interaction,
desktop/mobile layout and restored charts using fixtures and blocked remote data.
Also run the term regressions in `docs/term-rules.md` and the complete offline
suite. These checks do not guarantee every real LLM answer or a deployed service.

Continuation snapshots retain queried grade evidence, chart state and current
section snapshots. The Continue SSE response and saved assistant turn carry the
same structured charts; repeating an already gathered query reuses its evidence.
The frontend replaces an updated comparison chart rather than appending duplicates.
Missing or empty chart updates preserve the existing chart.

Verified on 2026-10-12:

- Complete offline suite: **810 passed**, three optional browser checks skipped,
  two live/manual tests deselected.
- Required term regression command: **134 passed** (included in the offline suite).
- Zotistics/frontend tests with real Chrome enabled: **39 passed**. Synthetic
  fixtures cover desktop/mobile layout, expansion, restored history and Continue;
  external network requests are blocked during these browser tests.
- A separate live adapter check for I&C SCI 45C / KLEFSTAD, R. returned 12 matching
  classes, 2,079 total grade records and 2,066 A–F records, including 871 A records.
  Ranking's raw A rate is therefore 42.16%. This check used a temporary cache;
  it does not seed the project's ranking cache or verify every source query.
- `git diff --check` passed. No deployment or real-LLM output verification was
  performed for this increment.

## Files in this increment

New production files:

- `app/data/zotistics.py`: source adapter, identity/scope validation and cache.
- `app/agent/grade_evidence.py`: request evidence, fallback and chart attachment.
- `app/data/recommendations.py`: full candidate retrieval and snapshot ranking.
- `app/recommendations/__init__.py`, `features.py`, `ranking.py`: public API,
  feature conversion and pure scoring.

Modified production/configuration files:

- `app/data/db.py`, `professors.py`: shared retrieval and verified rating identity.
- `app/agent/tools.py`, `loop.py`: tool integration, primary selection and Continue.
- `app/data/sessions.py`, `app/routers/chat.py`: SSE/session chart persistence.
- `app/config.py`, `.env.example`: opt-in flag and weight/reliability configuration.
- `static/js/cards.js`, `chat.js`, `sessions.js`, `static/styles/components.css`:
  grade charts and replay/Continue presentation.
- `README.md`, `docs/term-rules.md`, this document: feature entry and term/source
  boundaries. Existing prompt drafts were retained; no new LLM prompt changes
  were introduced during this implementation.

New/extended tests and fixtures: `tests/test_zotistics_data.py`,
`test_agent_zotistics.py`, `test_grade_evidence_pipeline.py`,
`test_recommendation_ranking.py`, `test_recommendation_ranking_integration.py`,
`test_zotistics_cards.py`, and `tests/fixtures/zotistics/`.

## Phase 2 proposal

Let the LLM interpret preferences into a supported-feature weight dictionary.
Validate those weights with the same Python contract, retain their provenance,
and keep all numerical scoring and sorting in Python. Evaluate ranking quality
and coverage bias before changing the default activation or shrinkage parameters.
This proposal is not implemented in Phase 1.
