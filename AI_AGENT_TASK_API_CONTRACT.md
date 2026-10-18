# AI Agent Task API Contract

This connector follows the backend contract in
`slr-be/docs/AI_AGENT_TASK_API_CONTRACT.md`. The same token must not be reused
across reviewer and leader workspaces: reviewer tokens are reviewer-scoped,
while leader operations use a project-scoped token with the required leader
capability.

## Quality Assessment agent contract

The QA tools call `/api/agent/v1/quality` and use the canonical backend QA
service. Criteria and weights always come from the configured project
strategy. The connector never invents criteria, totals, or an official
decision.

| Scope | Required capabilities | Visibility and writes |
| --- | --- | --- |
| Reviewer | `quality:review` plus `reviewerId` | Assigned included papers and the caller's own assessment only |
| Leader comparison | `quality:leader` | Both submitted reviewer assessments, evidence, checks, and conflicts |
| Official resolution | `quality:leader`, `quality:submit-resolution` | Server-recomputed final High/Low resolution after explicit confirmation |

Exact quote, source asset, source revision, source hash, normalized Markdown
offsets, quote hash, and optional PDF page/coordinates form the evidence
contract. Exact source binding is canonical; `pdfHighlightCoordinates` is only
UI compatibility metadata. A reviewer must complete every configured criterion
before submission. The project cannot complete QA with fewer than two valid
reviewer assessments for an eligible paper.

## MCP tool mapping

Open-Science and Claude expose the same QA tools:

`get_quality_context`, `list_quality_tasks`, `get_quality_strategy`,
`get_quality_source`, `get_quality_assessment`, `save_quality_assessment`,
`check_quality_assessment`, `submit_quality_assessment`,
`analyze_quality_consensus`, `save_quality_resolution_draft`,
`submit_quality_resolution`, `start_quality_phase`, and
`complete_quality_phase`.

Assessment and resolution submission use a server-issued, one-time challenge
bound to the exact payload digest and an idempotency key. The MCP tool performs
that handshake after one explicit user confirmation and reports success only
from the backend response. Leader comparison must show every configured
criterion in a complete table with both reviewer values, scores, comments,
evidence, checks, recommendation, and status (`AGREE`, `CONFLICT`, or
`MISSING`).
