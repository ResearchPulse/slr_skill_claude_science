# Screening rubric and output format

## Decision rules

Apply in this order:

1. **Hard exclusion first.** Check each exclusion criterion. If the paper clearly meets one
   → `EXCLUDE`. One clear hit is enough; still mention any others you noticed.
2. **Inclusion check.** For each inclusion criterion mark it `met`, `not_met` or
   `unclear`, with evidence.
   - Any `not_met` (clearly) → `EXCLUDE`.
   - All `met` → `INCLUDE`.
   - Otherwise (some `unclear`, none `not_met`) → `UNCERTAIN`.
3. **Task instructions win.** If the task `instructions` add or tighten rules (e.g.
   "exclude conference abstracts", "title/abstract screening only"), follow them.
4. **When torn between INCLUDE and EXCLUDE, choose UNCERTAIN.** A wrongly excluded paper
   is lost forever; an UNCERTAIN gets a human look.

Common hard-exclusion signals: wrong population or age group, non-human/animal study when
humans are required, wrong study design (review, editorial, protocol, commentary when
primary studies are required), publication date or language outside range, duplicate
publication, no relevant outcome reported.

## Confidence

`confidence` in `data` is 0–1:

- 0.9–1.0 explicit statements in the paper settle every criterion
- 0.7–0.89 decision is solid but one criterion relies on inference
- 0.5–0.69 judgement call (usually pairs with UNCERTAIN)
- below 0.5 do not use INCLUDE/EXCLUDE; vote UNCERTAIN

## Reasoning template (reasoning file, Markdown, ideally < 2,500 chars)

Always start with the `Decision:`, `Basis:` and `Key reason:` lines — the `drafts`
summary reads `Key reason:` to build the review table.

```
Decision: EXCLUDE
Basis: full text   (or: title + abstract only — full text unavailable)

Key reason: Study population is adolescents 12–17; inclusion criterion requires adults ≥18.

Criteria:
- IC1 Adults with type 2 diabetes — NOT MET: "participants aged 12–17" (Methods).
- IC2 RCT design — MET: randomized, parallel-group (Methods).
- EC1 Non-English — not triggered.

Notes: Secondary outcomes are relevant but cannot override IC1.
```

Keep it factual and traceable: each criterion line points to where in the paper the
evidence came from. Claude Science's reviewer agent checks claims against sources, so an
unsupported statement will be flagged.

## `data` object (JSON file)

```json
{
  "basis": "full_text",
  "confidence": 0.9,
  "studyDesign": "RCT",
  "population": "Adolescents 12-17 with T2D, n=240",
  "intervention": "...",
  "comparator": "...",
  "outcomes": ["HbA1c at 24 weeks"],
  "criteria": [
    {"id": "IC1", "text": "Adults with T2D", "status": "not_met", "evidence": "participants aged 12-17 (Methods)"},
    {"id": "IC2", "text": "RCT", "status": "met", "evidence": "randomized parallel-group (Methods)"}
  ],
  "exclusionTriggered": ["IC1 not met"],
  "evidence": ["Methods: participants aged 12-17 years"]
}
```

`basis` is `full_text`, `partial_full_text` (fullTextStatus `PARTIAL`) or
`title_abstract`. Use `null` for fields the paper does not report. Keep evidence snippets
short (one line each) so the body stays well under 30 KB.

Criterion ids: number the project's criteria in the order the API returns them
(`IC1, IC2…` for inclusionCriteria, `EC1, EC2…` for exclusionCriteria).
