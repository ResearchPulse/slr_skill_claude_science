#!/usr/bin/env python3
"""MCP server (stdio) for the SLR Agent Task API — the `slr` connector for Claude Science.

Claude Science launches it as a local custom connector on the host, so it always reaches the
backend and the real ~/.config/slr, even when the agent's shell is sandboxed.
It is the only channel the chat needs: API calls, configuration, and the local drafts +
audit log (~/slr-paper-review/<profile>/, or $REVIEW_WORKDIR). Claude Science agents may not run
scripts or read files inside the skill folder, so nothing in the skill depends on that.

Stdlib only. Config is read on every call from slr.py's stored profile, so
`/slr config token|url|use` takes effect immediately without reconnecting the connector.
Env overrides (API_BASE_URL, AGENT_TOKEN, REVIEW_PROFILE) still win if set.

Tools:
  connection_test()                                   check URL + token, task counts
  list_tasks(status?)                                 tasks + project context
  list_consensus_tasks()                              leader extraction tasks (auto-provisioned after R1/R2 submit)
  get_task(task_id)                                   one task / status / result
  get_markdown(task_id, max_wait?)                    full text (FULL_TEXT tasks only)
  save_draft(task_id, decision, reasoning, data, title?)  validate + save/replace a local draft
  list_drafts()                                       pending drafts table + submitted ids
  get_draft(task_id)                                  one saved draft (exact body)
  submit_drafts(task_ids?)                            revalidate stage/source, then POST approved drafts
  submit_result(task_id, decision, reasoning, data)   POST an advisory recommendation
  get_extraction_context(task_id)                     template, assignment and isolated draft
  get_extraction_fulltext(task_id)                    normalized Markdown with source coordinates
  save_extraction_draft(task_id, draft)               save isolated field/evidence draft
  check_extraction_draft(task_id)                     deterministic PASS/WARN/BLOCK validation
  submit_extraction_draft(task_id)                    confirmed direct canonical reviewer submission
  analyze_extraction_consensus(task_id?)               leader-only R1/R2 comparison and evidence view
  save_extraction_consensus_draft(task_id, draft)      leader-only challenged consensus draft
  submit_extraction_consensus(task_id, expectedVersion) leader-only official consensus submission
  finalize_extraction_consensus(task_id, proposal)     legacy leader-only consensus proposal
  config_show()                                       profiles, token masked
  config_set_url(url, profile?)                       save API base URL
  config_set_token(token, profile?)                   save token + test (never echoed)
  config_use(profile)                                 switch active profile
"""
import json
import os
import sys
import traceback
import urllib.parse
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import slr  # noqa: E402
from slr import (ApiError, Client, active_profile, build_body, drafts_table,  # noqa: E402
                 get_draft, mark_submitted, save_draft, set_token, set_url, show_config,
                 submit_drafts, use_profile)

# Drafts live on the host, one folder per profile (the chat's working folder is not visible here).
slr.WORKDIR_DEFAULT = lambda: Path.home() / "slr-paper-review" / active_profile()

SERVER_INFO = {"name": "slr", "version": "1.0.1"}
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

_OBJ = {"type": "object", "additionalProperties": False}
_TASK_ID = {"task_id": {"type": "string", "description": "Task id from list_tasks"}}
_PAPER_ID = {"paper_id": {"type": "string", "description": "Included QA paper id from list_quality_tasks"}}
_PROFILE = {"profile": {"type": "string", "description": "Profile name; default = active profile"}}

# Mirrors slr.validate_data; data.criteria feeds the web app's "Criteria assessment" panel.
DATA_SCHEMA = {
    "type": "object",
    "description": ("Structured assessment. For TITLE_ABSTRACT, use title, abstract and known "
                    "paper.keywords; null keywords are unavailable and must not be inferred or "
                    "treated as an exclusion. criteria must cover every inclusion (IC1…) and exclusion "
                    "(EC1…) criterion of the task's protocolSnapshot, in API order. Also include studyDesign, "
                    "population, intervention, comparator, outcomes, exclusionTriggered and evidence "
                    "(see 'Vote format' in the /slr skill)."),
    "properties": {
        "basis": {"type": "string", "enum": ["title_abstract", "full_text"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "criteria": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "IC1, IC2… / EC1, EC2…"},
                    "text": {"type": "string", "description": "The criterion text from protocolSnapshot"},
                    "status": {"type": "string", "enum": ["met", "not_met", "unclear"],
                               "description": "For EC: met = exclusion triggered"},
                    "evidence": {"type": "string",
                                 "description": "Short quote/finding with location, e.g. 'participants aged 12-17 (Methods)'"},
                },
                "required": ["id", "text", "status", "evidence"],
            },
        },
    },
    "required": ["basis", "confidence", "criteria"],
}

EXTRACTION_EVIDENCE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "fieldId": {"type": "string", "description": "Exact field id from the template snapshot"},
        "sourceAssetId": {"type": "string", "description": "Full-text source asset id"},
        "sourceRevision": {"type": "string", "description": "Full-text source revision"},
        "sourceHash": {"type": "string", "pattern": "^[a-f0-9]{64}$", "description": "SHA-256 hex"},
        "startOffset": {"type": "integer", "minimum": 0, "description": "Inclusive offset in normalized Markdown"},
        "endOffset": {"type": "integer", "minimum": 1, "description": "Exclusive offset in normalized Markdown"},
        "quote": {"type": "string", "minLength": 1, "description": "Exact bounded Markdown substring"},
        "quoteHash": {"type": "string", "pattern": "^[a-f0-9]{64}$", "description": "SHA-256 hex of quote"},
        "section": {"type": "string"},
        "paragraph": {"type": "integer", "minimum": 0},
        "pdfPage": {"type": "integer", "minimum": 1},
    },
    "required": ["fieldId", "sourceAssetId", "sourceRevision", "sourceHash", "startOffset",
                  "endOffset", "quote", "quoteHash"],
}

EXTRACTION_DRAFT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "description": "Canonical isolated Phase-2 extraction draft; use the template and full-text values exactly.",
    "properties": {
        "templateId": {"type": "string", "description": "Template id from extraction context"},
        "templateVersion": {"type": "integer", "minimum": 1, "description": "Numeric template version"},
        "sourceAssetId": {"type": "string", "description": "Full-text source asset id"},
        "sourceRevision": {"type": "string", "description": "Full-text source revision"},
        "sourceHash": {"type": "string", "pattern": "^[a-f0-9]{64}$", "description": "SHA-256 hex"},
        "expectedVersion": {"type": "integer", "minimum": 0, "description": "Isolated draft version, or 0"},
        "values": {"type": "object", "description": "Object keyed by exact template field ids"},
        "evidence": {"type": "array", "items": EXTRACTION_EVIDENCE_SCHEMA,
                     "description": "Source-bound evidence records; use [] when none is defensible"},
    },
    "required": ["templateId", "templateVersion", "sourceAssetId", "sourceRevision", "sourceHash",
                  "expectedVersion", "values", "evidence"],
}

CONSENSUS_ANALYSIS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "description": ("Complete comparison for every custom template field. Use AGREE only for exact "
                    "canonical equality; SEMANTIC_AGREE only when both values mean the same thing; "
                    "CONFLICT when Leader must decide; MISSING when a reviewer omitted a value."),
    "properties": {
        "schemaVersion": {"type": "string", "enum": ["leader-consensus-v1"]},
        "status": {"type": "string", "enum": ["READY", "CONFLICT"]},
        "summary": {"type": "string", "minLength": 1},
        "generatedAt": {"type": "string"},
        "sourceRevision": {"type": "string"},
        "sourceHash": {"type": "string"},
        "fields": {"type": "array", "minItems": 1, "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "fieldId": {"type": "string"},
                "fieldName": {"type": "string"},
                "sectionName": {"type": "string"},
                "isRequired": {"type": "boolean"},
                "status": {"type": "string", "enum": ["AGREE", "SEMANTIC_AGREE", "CONFLICT", "MISSING"]},
                "reviewer1Value": {},
                "reviewer2Value": {},
                "reviewer1Evidence": {"type": "array", "items": EXTRACTION_EVIDENCE_SCHEMA},
                "reviewer2Evidence": {"type": "array", "items": EXTRACTION_EVIDENCE_SCHEMA},
                "recommendedValue": {},
                "reason": {"type": "string", "minLength": 1},
                "checks": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["fieldId", "status", "reason"],
        }},
    },
    "required": ["schemaVersion", "status", "summary", "fields"],
}

CONSENSUS_FINDING_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "findingId": {"type": "string"},
        "fieldId": {"type": "string"},
        "kind": {"type": "string", "enum": ["VALUE_DISAGREEMENT", "MISSING_REQUIRED", "AI_SUGGESTION"]},
        "status": {"type": "string", "enum": ["PENDING", "RESOLVED", "DISMISSED"]},
        "reviewer1Value": {}, "reviewer2Value": {}, "proposedValue": {}, "resolution": {},
        "reason": {"type": "string"},
    },
    "required": ["findingId", "fieldId", "kind", "status"],
}

CONSENSUS_DRAFT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "description": ("Leader consensus draft. Include every template field in values, every field in "
                    "analysis.fields, and findings for disagreements. Both reviewer submissions must "
                    "already be SUBMITTED."),
    "properties": {
        "expectedVersion": {"type": "integer", "minimum": 0},
        "values": {"type": "object", "description": "Final candidate values keyed by exact template field ids"},
        "findings": {"type": "array", "items": CONSENSUS_FINDING_SCHEMA},
        "analysis": CONSENSUS_ANALYSIS_SCHEMA,
    },
    "required": ["expectedVersion", "values", "findings", "analysis"],
}

QUALITY_EVIDENCE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "criterionId": {"type": "string", "description": "Must match the parent decision item's qualityCriterionId"},
        "quote": {"type": "string", "minLength": 1, "description": "Exact bounded substring of get_quality_source.markdown"},
        "sourceAssetId": {"type": "string", "description": "Copied unchanged from get_quality_source"},
        "sourceRevision": {"type": "string", "description": "Copied unchanged from get_quality_source"},
        "sourceHash": {"type": "string", "pattern": "^[a-f0-9]{64}$", "description": "Copied unchanged from get_quality_source"},
        "startOffset": {"type": "integer", "minimum": 0},
        "endOffset": {"type": "integer", "minimum": 1},
        "quoteHash": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "pdfPage": {"type": "integer", "minimum": 1},
        "section": {"type": "string"},
    },
    "required": ["criterionId", "quote", "sourceAssetId", "sourceRevision", "sourceHash", "quoteHash"],
}
QUALITY_DECISION_ITEM_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "qualityCriterionId": {"type": "string"},
        "value": {"type": "integer", "enum": [0, 1, 2], "description": "0 Yes, 1 No, 2 Unclear"},
        "comment": {"type": ["string", "null"]},
        "pdfHighlightCoordinates": {"type": ["string", "null"]},
        "evidence": {"type": "array", "items": QUALITY_EVIDENCE_SCHEMA},
    },
    "required": ["qualityCriterionId", "value"],
}
QUALITY_ASSESSMENT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "notes": {"type": ["string", "null"]},
        "decisionItems": {"type": "array", "items": QUALITY_DECISION_ITEM_SCHEMA, "minItems": 1,
                          "description": "Exactly one item per criterion returned by get_quality_strategy"},
    },
    "required": ["decisionItems"],
}


def _validate_quality_assessment_draft(draft):
    """Reject malformed QA drafts before they become a misleading missing-draft error."""
    if not isinstance(draft, dict):
        raise ApiError(None, "INVALID_QA_DRAFT", "draft must be an object with decisionItems.")
    legacy = sorted(set(draft).intersection({"fields", "criteria", "values", "assessment"}))
    if legacy:
        raise ApiError(None, "INVALID_QA_DRAFT",
                       "Use draft.decisionItems, not legacy key(s) " + ", ".join(legacy) + ".")
    items = draft.get("decisionItems")
    if not isinstance(items, list) or not items:
        raise ApiError(None, "INVALID_QA_DRAFT",
                       "draft.decisionItems must contain one scored item for every QA criterion; "
                       "do not call save with an empty probe payload.")
    seen = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ApiError(None, "INVALID_QA_DRAFT", f"decisionItems[{index}] must be an object.")
        criterion_id = item.get("qualityCriterionId")
        if not isinstance(criterion_id, str) or not criterion_id.strip():
            raise ApiError(None, "INVALID_QA_DRAFT",
                           f"decisionItems[{index}].qualityCriterionId is required.")
        if criterion_id in seen:
            raise ApiError(None, "INVALID_QA_DRAFT", f"Duplicate qualityCriterionId: {criterion_id}.")
        seen.add(criterion_id)
        if item.get("value") not in (0, 1, 2):
            raise ApiError(None, "INVALID_QA_DRAFT",
                           f"decisionItems[{index}].value must be 0 (Yes), 1 (No), or 2 (Unclear).")
        evidence = item.get("evidence", [])
        if evidence is not None and not isinstance(evidence, list):
            raise ApiError(None, "INVALID_QA_DRAFT",
                           f"decisionItems[{index}].evidence must be an array.")
    return draft

TOOLS = [
    {
        "name": "connection_test",
        "title": "Test SLR API connection",
        "description": ("Check that the connector reaches the API with the active profile's token; "
                        "returns profile, project name and assigned/completed counts."),
        "inputSchema": {**_OBJ, "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "list_tasks",
        "title": "List SLR tasks",
        "description": ("List tasks visible to the active agent token (all cursor pages), "
                        "including stage, instructions, protocolSnapshot and paper metadata "
                        "(title, abstract, keywords, fullTextStatus). Keywords are an array or "
                        "null when unavailable; never infer missing keywords. The page also includes the "
                        "current project context."),
        "inputSchema": {**_OBJ, "properties": {
            "status": {"type": "string", "enum": ["ASSIGNED", "COMPLETED", "any"], "default": "ASSIGNED"},
            "stage": {"type": "string", "enum": ["TITLE_ABSTRACT", "FULL_TEXT"]}}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "list_consensus_tasks",
        "title": "List leader consensus tasks",
        "description": ("Leader-token only. List server-owned extraction consensus tasks for the active "
                        "project. Tasks are provisioned automatically after both assigned reviewers "
                        "submit, so the caller does not need to know or copy a task id."),
        "inputSchema": {**_OBJ, "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "get_task",
        "title": "Get SLR task",
        "description": ("Return one task with stage, protocolSnapshot, markdownPath, paper metadata "
                        "including keywords as an array or null when unavailable, "
                        "and result. Use it to refresh a task or verify a submission landed "
                        "(status COMPLETED)."),
        "inputSchema": {**_OBJ, "properties": _TASK_ID, "required": ["task_id"]},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "get_markdown",
        "title": "Get paper full text",
        "description": ("Full-text Markdown for a FULL_TEXT task that has markdownPath. Never call for "
                        "TITLE_ABSTRACT tasks. Waits per Retry-After while extraction is pending (up "
                        "to max_wait seconds). Returns the Markdown text, or status pending / "
                        "unavailable."),
        "inputSchema": {**_OBJ, "properties": {
            **_TASK_ID,
            "max_wait": {"type": "integer", "default": 30, "minimum": 0, "maximum": 120}},
            "required": ["task_id"]},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "submit_result",
        "title": "Submit SLR screening recommendation",
        "description": (
            "Submit an advisory recommendation for one screening task. TITLE_ABSTRACT accepts "
            "ADVANCE_TO_FULL_TEXT, EXCLUDE or UNCERTAIN; FULL_TEXT accepts INCLUDE, EXCLUDE or "
            "UNCERTAIN. The result completes only the AI task, never a human vote. Call only after "
            "the scientist approved this exact recommendation."
        ),
        "inputSchema": {**_OBJ, "properties": {
            **_TASK_ID,
            "decision": {"type": "string", "description": "Stage-specific advisory recommendation", "maxLength": 100},
            "reasoning": {"type": "string", "description": "Reasoning text (max 16,000 chars)"},
            "data": DATA_SCHEMA},
            "required": ["task_id", "decision", "reasoning", "data"]},
        "annotations": {"destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
    },
    {
        "name": "get_extraction_context", "title": "Get extraction task context",
        "description": "Read the assigned extraction task, immutable template snapshot, paper metadata and isolated agent draft.",
        "inputSchema": {**_OBJ, "properties": _TASK_ID, "required": ["task_id"]},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "get_extraction_fulltext", "title": "Get extraction full text",
        "description": "Read normalized Markdown, source asset/revision/hash, and paragraph offset coordinates.",
        "inputSchema": {**_OBJ, "properties": _TASK_ID, "required": ["task_id"]},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "save_extraction_draft", "title": "Save isolated extraction draft",
        "description": ("Save the canonical isolated Phase-2 draft. First read extraction context and full text. "
                        "draft must contain templateId, numeric templateVersion, sourceAssetId, string sourceRevision, "
                        "64-hex sourceHash, expectedVersion, values object keyed by exact field ids, and an evidence "
                        "array. Each evidence record must carry exact normalized-Markdown offsets, quote and quoteHash. "
                        "Do not use legacy fields/baseVersion/v1 shapes. Canonical reviewer data is untouched."),
        "inputSchema": {**_OBJ, "properties": {**_TASK_ID, "draft": EXTRACTION_DRAFT_SCHEMA},
                         "required": ["task_id", "draft"]},
        "annotations": {"idempotentHint": True},
    },
    {
        "name": "check_extraction_draft", "title": "Check extraction draft",
        "description": "Run deterministic value and evidence checks. Returns versioned PASS, WARN or BLOCK.",
        "inputSchema": {**_OBJ, "properties": _TASK_ID, "required": ["task_id"]},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "analyze_extraction_consensus", "title": "Compare reviewer extraction consensus",
        "description": ("Leader-token only. Read R1 and R2 submissions after both are SUBMITTED. "
                        "task_id is optional when exactly one leader consensus task is visible; "
                        "Returns every custom template field, both values, evidence quotes/offsets, "
                        "value/evidence checks, agreement status and conflict reasons. Never writes."),
        "inputSchema": {**_OBJ, "properties": _TASK_ID},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "save_extraction_consensus_draft", "title": "Save leader consensus draft",
        "description": ("Leader-token only. Compare all R1/R2 custom fields, then save the complete "
                        "consensus draft with analysis and findings. Both reviewers must be submitted. "
                        "Requires explicit user approval; CONFLICT is saved as a draft but cannot become official."),
        "inputSchema": {**_OBJ, "properties": {**_TASK_ID, "draft": CONSENSUS_DRAFT_SCHEMA},
                         "required": ["task_id", "draft"]},
        "annotations": {"destructiveHint": True, "idempotentHint": True, "openWorldHint": True},
    },
    {
        "name": "submit_extraction_consensus", "title": "Submit official extraction consensus",
        "description": ("Leader-token only. Submit the saved consensus draft as the official project "
                        "consensus. Allowed only when R1 and R2 submitted and every field is AGREE or "
                        "SEMANTIC_AGREE with no unresolved conflict. Requires explicit user approval."),
        "inputSchema": {**_OBJ, "properties": {**_TASK_ID,
                         "expectedVersion": {"type": "integer", "minimum": 1}},
                         "required": ["task_id", "expectedVersion"]},
        "annotations": {"destructiveHint": True, "idempotentHint": True, "openWorldHint": True},
    },
    {
        "name": "submit_extraction_draft", "title": "Submit extraction draft",
        "description": "After reviewing the draft and a non-blocking check, create a one-time challenge and submit directly to the canonical reviewer record. Requires one explicit user confirmation. Returns SUBMITTED; consensus still requires the leader workflow.",
        "inputSchema": {**_OBJ, "properties": _TASK_ID, "required": ["task_id"]},
        "annotations": {"destructiveHint": True, "idempotentHint": True, "openWorldHint": True},
    },
    {
        "name": "finalize_extraction_consensus", "title": "Propose extraction consensus",
        "description": "Leader-token only. Create or update a challenged consensus proposal; this does not finalize canonical consensus. Requires one explicit user confirmation.",
        "inputSchema": {**_OBJ, "properties": {**_TASK_ID, "proposal": {"type": "object"}},
                         "required": ["task_id", "proposal"]},
        "annotations": {"destructiveHint": True, "idempotentHint": True, "openWorldHint": True},
    },
    {
        "name": "get_quality_context", "title": "Get QA context",
        "description": "Read the role-scoped Quality Assessment process, strategy, capabilities and assignments.",
        "inputSchema": {**_OBJ, "properties": {}}, "annotations": {"readOnlyHint": True},
    },
    {
        "name": "list_quality_tasks", "title": "List QA tasks",
        "description": "List reviewer-assigned or leader-visible included papers with reviewer completion status. Reviewer tokens never receive peer values.",
        "inputSchema": {**_OBJ, "properties": {}}, "annotations": {"readOnlyHint": True},
    },
    {
        "name": "get_quality_strategy", "title": "Get QA strategy",
        "description": "Return the canonical QA checklists, criteria, questions and weights.",
        "inputSchema": {**_OBJ, "properties": {}}, "annotations": {"readOnlyHint": True},
    },
    {
        "name": "get_quality_source", "title": "Get QA source text",
        "description": "Read the immutable Cloudinary-backed full-text Markdown and source hash used for exact QA evidence.",
        "inputSchema": {**_OBJ, "properties": _PAPER_ID, "required": ["paper_id"]}, "annotations": {"readOnlyHint": True},
    },
    {
        "name": "get_quality_assessment", "title": "Get QA assessment",
        "description": "Reviewer: own assessment and draft only. Leader: both submitted reviewer assessments for one paper.",
        "inputSchema": {**_OBJ, "properties": _PAPER_ID, "required": ["paper_id"]}, "annotations": {"readOnlyHint": True},
    },
    {
        "name": "save_quality_assessment", "title": "Save QA assessment draft",
        "description": ("Reviewer-only. Save or replace your own QA draft without changing the canonical submission. "
                        "draft MUST be {notes?, decisionItems:[{qualityCriterionId,value,comment?,pdfHighlightCoordinates?,evidence?}]}; "
                        "include exactly one decisionItems entry per configured criterion. Do not use fields, criteria, values, "
                        "assessment, or an empty probe payload."),
        "inputSchema": {**_OBJ, "properties": {**_PAPER_ID, "draft": QUALITY_ASSESSMENT_SCHEMA}, "required": ["paper_id", "draft"]},
        "annotations": {"idempotentHint": True},
    },
    {
        "name": "check_quality_assessment", "title": "Check QA assessment",
        "description": "Reviewer-only. Validate all criteria, exact quote/source hash evidence and source offsets before submission.",
        "inputSchema": {**_OBJ, "properties": _PAPER_ID, "required": ["paper_id"]}, "annotations": {"readOnlyHint": True},
    },
    {
        "name": "submit_quality_assessment", "title": "Submit QA assessment",
        "description": "Reviewer-only and irreversible for the current draft version. Requires explicit user approval; returns backend-confirmed SUBMITTED status.",
        "inputSchema": {**_OBJ, "properties": _PAPER_ID, "required": ["paper_id"]},
        "annotations": {"destructiveHint": True, "idempotentHint": True, "openWorldHint": True},
    },
    {
        "name": "analyze_quality_consensus", "title": "Analyze QA consensus",
        "description": "Leader-only. Compare every configured criterion, both reviewer answers, comments, exact evidence, source checks and conflict reasons. Never writes.",
        "inputSchema": {**_OBJ, "properties": _PAPER_ID, "required": ["paper_id"]}, "annotations": {"readOnlyHint": True},
    },
    {
        "name": "save_quality_resolution_draft", "title": "Save QA resolution draft",
        "description": "Leader-only. Store a recommendation/analysis draft; it is not the official project resolution.",
        "inputSchema": {**_OBJ, "properties": {**_PAPER_ID, "draft": {"type": "object"}}, "required": ["paper_id", "draft"]},
        "annotations": {"idempotentHint": True},
    },
    {
        "name": "submit_quality_resolution", "title": "Submit official QA resolution",
        "description": "Leader-only. Submit the explicit final High/Low decision after reviewing the full comparison. The backend recomputes the score and records audit metadata.",
        "inputSchema": {**_OBJ, "properties": {**_PAPER_ID, "finalDecision": {"type": "integer", "enum": [0, 1]}, "resolutionNotes": {"type": ["string", "null"]}}, "required": ["paper_id", "finalDecision"]},
        "annotations": {"destructiveHint": True, "idempotentHint": True, "openWorldHint": True},
    },
    {
        "name": "start_quality_phase", "title": "Start QA phase",
        "description": "Leader-only. Start Quality Assessment when canonical Study Selection prerequisites and criteria are ready.",
        "inputSchema": {**_OBJ, "properties": {}}, "annotations": {"destructiveHint": True, "idempotentHint": True},
    },
    {
        "name": "complete_quality_phase", "title": "Complete QA phase",
        "description": "Leader-only. Complete QA only after every included paper has two complete reviewer assessments and an official resolution.",
        "inputSchema": {**_OBJ, "properties": {}}, "annotations": {"destructiveHint": True, "idempotentHint": True, "openWorldHint": True},
    },
    {
        "name": "save_draft",
        "title": "Save SLR draft vote",
        "description": ("Validate and save (or replace) the draft vote for one task on the host. Does NOT "
                        "submit. Same body as submit_result; data must follow the rubric in the skill "
                        "(basis, confidence, criteria for every IC/EC). Use it for /slr review and "
                        "/slr edit."),
        "inputSchema": {**_OBJ, "properties": {
            **_TASK_ID,
            "title": {"type": "string", "description": "Short paper title for the drafts table"},
            "decision": {"type": "string",
                         "description": ("TITLE_ABSTRACT: ADVANCE_TO_FULL_TEXT, EXCLUDE or UNCERTAIN; "
                                         "FULL_TEXT: INCLUDE, EXCLUDE or UNCERTAIN"),
                         "maxLength": 100},
            "reasoning": {"type": "string", "description": "Reasoning text (max 16,000 chars)"},
            "data": DATA_SCHEMA},
            "required": ["task_id", "decision", "reasoning", "data"]},
        "annotations": {"idempotentHint": True},
    },
    {
        "name": "list_drafts",
        "title": "List SLR drafts",
        "description": ("Pending drafts (task id, title, decision, confidence, key reason), counts by "
                        "decision, and ids already submitted from this profile."),
        "inputSchema": {**_OBJ, "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "get_draft",
        "title": "Get SLR draft",
        "description": "Return one saved draft (pending or submitted) with its exact body.",
        "inputSchema": {**_OBJ, "properties": _TASK_ID, "required": ["task_id"]},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "submit_drafts",
        "title": "Submit approved SLR drafts",
        "description": ("Refresh each task and validate stage-specific decision and source status before POST; "
                        "move each accepted draft to submitted. "
                        "IRREVERSIBLE. Only call after the scientist explicitly approved these drafts. "
                        "task_ids = the approved ids (omit only when every pending draft was approved). "
                        "Stops the batch on 401. Per task: submitted / already_completed / error."),
        "inputSchema": {**_OBJ, "properties": {
            "task_ids": {"type": "array", "items": {"type": "string"}}}},
        "annotations": {"destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
    },
    {
        "name": "config_show",
        "title": "Show SLR profiles",
        "description": "List profiles with API URL and masked token, and which one is active.",
        "inputSchema": {**_OBJ, "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "config_set_url",
        "title": "Set SLR API URL",
        "description": "Save the backend API base URL (/api appended if missing) for a profile.",
        "inputSchema": {**_OBJ, "properties": {"url": {"type": "string"}, **_PROFILE}, "required": ["url"]},
        "annotations": {"idempotentHint": True},
    },
    {
        "name": "config_set_token",
        "title": "Set SLR agent token",
        "description": ("Save the reviewer-workspace agent token the user gave for a profile (file "
                        "with 0600 permissions on the host), then test the connection. The token is "
                        "never returned — the result shows only a masked form."),
        "inputSchema": {**_OBJ, "properties": {"token": {"type": "string"}, **_PROFILE}, "required": ["token"]},
        "annotations": {"idempotentHint": True},
    },
    {
        "name": "config_use",
        "title": "Switch SLR profile",
        "description": "Make another saved profile (project) the active one, then test it.",
        "inputSchema": {**_OBJ, "properties": {"profile": {"type": "string"}}, "required": ["profile"]},
        "annotations": {"idempotentHint": True},
    },
]


def log(msg):
    print(f"[slr-mcp] {msg}", file=sys.stderr, flush=True)


def _markdown_cell(value):
    if value is None:
        text = "—"
    elif isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    elif isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    return text.replace("|", r"\|").replace("\r", " ").replace("\n", "<br>")


def _markdown_table(rows):
    normalized = []
    for row in rows or []:
        normalized.append(row if isinstance(row, dict) else {"value": row})
    columns = []
    for row in normalized:
        for key in row:
            key = str(key)
            if key not in columns:
                columns.append(key)
    if not columns:
        columns = ["value"]
    header = "| " + " | ".join(_markdown_cell(column) for column in columns) + " |"
    separator = "|" + "|".join("---" for _ in columns) + "|"
    body = [
        "| " + " | ".join(_markdown_cell(row.get(column)) for column in columns) + " |"
        for row in normalized
    ]
    return "\n".join([header, separator, *body])


def _markdown_content(payload):
    if isinstance(payload, dict):
        summary = []
        tables = []
        for key, value in payload.items():
            if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
                tables.append((key, value))
            else:
                summary.append({"field": key, "value": value})
        parts = [_markdown_table(summary)] if summary else []
        parts.extend(f"### {title}\n\n{_markdown_table(rows)}" for title, rows in tables)
        return "\n\n".join(parts) or _markdown_table([])
    if isinstance(payload, list):
        return _markdown_table(payload)
    return _markdown_table([{"value": payload}])


def _consensus_root(payload):
    """Find a leader-consensus response without hiding nested analysis data."""
    if not isinstance(payload, dict):
        return None
    if payload.get("schemaVersion") == "leader-consensus-v1":
        return payload
    for key in ("consensus", "result", "leaderDraft", "payload"):
        child = payload.get(key)
        found = _consensus_root(child)
        if found:
            return found
    return None


def _consensus_markdown(payload):
    root = _consensus_root(payload) or payload
    parts = []
    summary = []
    for key in ("taskId", "projectId", "paperId", "status", "message", "missingReviewerIds"):
        if key in root:
            summary.append({"field": key, "value": root.get(key)})
    if isinstance(payload, dict) and payload is not root:
        for key in ("operation", "official", "canonicalConsensusFinalized", "confirmationConsumed"):
            if key in payload:
                summary.append({"field": key, "value": payload.get(key)})
    if summary:
        parts.append("## Consensus overview\n\n" + _markdown_table(summary))

    reviewer_statuses = root.get("reviewerStatuses")
    if isinstance(reviewer_statuses, list):
        parts.append("## Reviewer submission status\n\n" + _markdown_table(reviewer_statuses))

    analysis = root.get("analysis")
    if not isinstance(analysis, dict):
        draft = root.get("leaderDraft")
        if isinstance(draft, dict):
            analysis = draft.get("agentAnalysis")
    if isinstance(analysis, dict):
        fields = analysis.get("fields") or []
        field_rows = []
        evidence_rows = []
        for field in fields:
            if not isinstance(field, dict):
                continue
            r1_evidence = field.get("reviewer1Evidence") or []
            r2_evidence = field.get("reviewer2Evidence") or []
            field_rows.append({
                "Field": field.get("fieldName") or field.get("fieldId"),
                "Section": field.get("sectionName"),
                "Field ID": field.get("fieldId"),
                "Required": field.get("isRequired"),
                "Status": field.get("status"),
                "R1 value": field.get("reviewer1Value"),
                "R2 value": field.get("reviewer2Value"),
                "Recommended": field.get("recommendedValue"),
                "R1 evidence": len(r1_evidence),
                "R2 evidence": len(r2_evidence),
                "Value/evidence checks": field.get("checks") or [],
                "Reason": field.get("reason"),
            })
            for reviewer, items in (("R1", r1_evidence), ("R2", r2_evidence)):
                for item in items:
                    if isinstance(item, dict):
                        evidence_rows.append({
                            "Field": field.get("fieldName") or field.get("fieldId"),
                            "Field ID": field.get("fieldId"),
                            "Reviewer": reviewer,
                            "Quote": item.get("quote"),
                            "Section": item.get("section"),
                            "Paragraph": item.get("paragraph"),
                            "PDF page": item.get("pdfPage"),
                        })
        parts.append("## Field-by-field value checks\n\n" + _markdown_table(field_rows))
        if not evidence_rows:
            evidence_rows = [{"Field": "—", "Field ID": "—", "Reviewer": "—", "Quote": "—",
                              "Section": "—", "Paragraph": "—", "PDF page": "—"}]
        parts.append("## Evidence for each field\n\n" + _markdown_table(evidence_rows))
        parts.append("## Analysis summary\n\n" + _markdown_table([
            {"field": "analysis status", "value": analysis.get("status")},
            {"field": "summary", "value": analysis.get("summary")},
            {"field": "generated at", "value": analysis.get("generatedAt")},
            {"field": "source asset", "value": root.get("source", {}).get("sourceAssetId") if isinstance(root.get("source"), dict) else None},
            {"field": "source revision", "value": analysis.get("sourceRevision")},
            {"field": "source hash", "value": analysis.get("sourceHash")},
        ]))

    submissions = root.get("reviewerSubmissions")
    if isinstance(submissions, list):
        rows = []
        for submission in submissions:
            if not isinstance(submission, dict):
                continue
            rows.append({
                "Reviewer": submission.get("reviewerId"),
                "Status": submission.get("status"),
                "Version": submission.get("version"),
                "Submitted at": submission.get("submittedAt"),
                "Value fields": len(submission.get("values") or {}),
                "Evidence items": len(submission.get("evidence") or []),
            })
        parts.append("## Reviewer payload summary\n\n" + _markdown_table(rows))

    findings = root.get("findings")
    if not isinstance(findings, list):
        draft = root.get("leaderDraft")
        findings = draft.get("findings") if isinstance(draft, dict) else None
    if isinstance(findings, list):
        parts.append("## Consensus findings\n\n" + _markdown_table(findings))
    return "\n\n".join(parts) or _markdown_content(payload)


def _is_quality_payload(payload):
    if not isinstance(payload, dict):
        return False
    analysis = payload.get("analysis")
    return (payload.get("profileScope") in {"REVIEWER", "LEADER"}
            or payload.get("role") in {"REVIEWER", "LEADER"}
            or isinstance(payload.get("reviewerStatuses"), list)
            or isinstance(payload.get("decisionItems"), list)
            or isinstance(analysis, dict) and analysis.get("schemaVersion") == "quality-consensus-v1")


def _quality_answer_label(value):
    """Render the QA answer semantically; numeric wire values stay internal."""
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"yes", "no", "unclear"}:
            return normalized.capitalize()
    return {0: "Yes", 1: "No", 2: "Unclear"}.get(value, value)


def _quality_markdown(payload):
    parts = []
    reviewer_scoped = payload.get("profileScope") == "REVIEWER" or payload.get("role") == "REVIEWER"
    summary = []
    for key in ("taskId", "projectId", "paperId", "paperTitle", "role", "status", "profileScope", "message"):
        if key in payload:
            summary.append({"Field": key, "Value": payload.get(key)})
    if isinstance(payload.get("project"), dict):
        summary.append({"Field": "Project", "Value": payload["project"].get("name") or payload["project"].get("id")})
    if summary:
        parts.append("## QA overview\n\n" + _markdown_table(summary))

    for key, title in (("reviewerStatuses", "Reviewer submission status"), ("items", "QA tasks"), ("strategies", "QA strategy")):
        if reviewer_scoped and key == "reviewerStatuses":
            continue
        value = payload.get(key)
        if isinstance(value, list):
            rows = []
            for item in value:
                if isinstance(item, dict):
                    if reviewer_scoped and key == "items":
                        own_assessments = item.get("assessments") or []
                        own = own_assessments[0] if own_assessments and isinstance(own_assessments[0], dict) else {}
                        rows.append({
                            "Task ID": item.get("taskId"),
                            "Paper": item.get("paperTitle") or item.get("paperId"),
                            "Status": item.get("status"),
                            "My submission": own.get("submitted", False),
                            "My completion": own.get("completion"),
                        })
                    else:
                        rows.append(item)
            if rows:
                parts.append(f"## {title}\n\n" + _markdown_table(rows))
        elif isinstance(value, dict):
            parts.append(f"## {title}\n\n" + _markdown_table([{"Section": key, "Value": value}]))

    analysis = payload.get("analysis")
    # Reviewer-scoped payloads must never render the leader's cross-review analysis,
    # which contains peer values, reviewer labels, and peer evidence.
    if isinstance(analysis, dict) and not reviewer_scoped:
        fields = analysis.get("fields") or []
        rows, evidence_rows = [], []
        for field in fields:
            if not isinstance(field, dict):
                continue
            r1 = field.get("reviewer1Evidence") or []
            r2 = field.get("reviewer2Evidence") or []
            rows.append({
                "Field": field.get("fieldName") or field.get("fieldId"),
                "Field ID": field.get("fieldId"),
                "Section": field.get("sectionName"),
                "Weight": field.get("weight"),
                "Status": field.get("status"),
                "R1 answer": _quality_answer_label(field.get("reviewer1Value")),
                "R2 answer": _quality_answer_label(field.get("reviewer2Value")),
                "R1 evidence": len(r1), "R2 evidence": len(r2),
                "Checks": field.get("checks") or [], "Reason": field.get("reason"),
            })
            for reviewer, evidence in (("R1", r1), ("R2", r2)):
                for item in evidence:
                    if isinstance(item, dict):
                        evidence_rows.append({
                            "Field": field.get("fieldName") or field.get("fieldId"),
                            "Reviewer": reviewer, "Exact quote": item.get("quote"),
                            "PDF page": item.get("pdfPage"),
                        })
        parts.append("## Criterion-by-criterion comparison\n\n" + _markdown_table(rows or [{"Field": "—", "Status": analysis.get("status")}]))
        parts.append("## Evidence\n\n" + _markdown_table(evidence_rows or [{"Field": "—", "Reviewer": "—", "Evidence": "Missing"}]))
        parts.append("## Analysis result\n\n" + _markdown_table([
            {"Field": "Status", "Value": analysis.get("status")},
            {"Field": "Summary", "Value": analysis.get("summary")},
            {"Field": "Generated at", "Value": analysis.get("generatedAt")},
        ]))

    assessments = payload.get("assessments")
    if isinstance(assessments, list):
        rows, evidence_rows = [], []
        for assessment in assessments:
            if not isinstance(assessment, dict):
                continue
            assessment_row = {"Name": assessment.get("reviewerName"), "Score": assessment.get("score"), "Notes": assessment.get("notes")}
            if not reviewer_scoped:
                assessment_row["Reviewer"] = assessment.get("reviewerId")
            rows.append(assessment_row)
            for item in assessment.get("decisionItems") or []:
                if not isinstance(item, dict):
                    continue
                item_row = {"Criterion": item.get("qualityCriterionId"), "Question": item.get("criterionQuestion"), "Answer": _quality_answer_label(item.get("value")), "Comment": item.get("comment"), "Evidence count": len(item.get("evidence") or [])}
                if not reviewer_scoped:
                    item_row["Reviewer"] = assessment.get("reviewerId")
                rows.append(item_row)
                for evidence in item.get("evidence") or []:
                    if isinstance(evidence, dict):
                        evidence_row = {"Criterion": item.get("qualityCriterionId"), "Exact quote": evidence.get("quote")}
                        if not reviewer_scoped:
                            evidence_row["Reviewer"] = assessment.get("reviewerId")
                        evidence_rows.append(evidence_row)
        parts.append("## Assessment values\n\n" + _markdown_table(rows or [{"Assessment": "None"}]))
        parts.append("## Assessment evidence\n\n" + _markdown_table(evidence_rows or [{"Evidence": "None"}]))

    if isinstance(payload.get("draft"), dict):
        draft = payload["draft"]
        parts.append("## Draft status\n\n" + _markdown_table([{"Version": draft.get("version"), "Status": draft.get("status"), "Updated": draft.get("updatedAt"), "Evidence source": draft.get("source")}]))
    return "\n\n".join(parts) or _markdown_content(payload)


def tool_result(payload, is_error=False):
    content = (_quality_markdown(payload) if _is_quality_payload(payload)
               else _consensus_markdown(payload) if _consensus_root(payload)
               else _markdown_content(payload))
    return {"content": [{"type": "text", "text": content}],
            "structuredContent": payload, "isError": is_error}


def connection_test():
    prof = active_profile()
    try:
        c = Client(retries=0)
        project, tasks = c.list_tasks(status="any")
    except ApiError as e:
        return {"connected": False, "profile": prof, "error": e.as_dict()}
    assigned = sum(1 for t in tasks if t.get("status") == "ASSIGNED")
    return {"connected": True, "profile": prof, "api": c.base, "project": (project or {}).get("name"),
            "assigned": assigned, "completed": len(tasks) - assigned}


def call_tool(name, args):
    try:
        if name == "connection_test":
            res = connection_test()
            return tool_result(res, not res["connected"])
        if name == "list_tasks":
            project, items = Client().list_tasks(status=args.get("status") or "ASSIGNED",
                                                 stage=args.get("stage"))
            return tool_result({"profile": active_profile(), "project": project,
                                "count": len(items), "items": items})
        if name == "list_consensus_tasks":
            project, items, readiness = Client().list_consensus_tasks(status="any")
            return tool_result({"profile": active_profile(), "project": project,
                                "count": len(items), "items": items,
                                "readiness": readiness,
                                "message": ("Leader extraction tasks are provisioned automatically "
                                             "after both assigned reviewers submit.")})
        if name == "get_task":
            return tool_result(Client().get_task(str(args["task_id"])))
        if name == "get_extraction_context":
            return tool_result(Client().extraction_request("GET", str(args["task_id"]), "context"))
        if name == "get_extraction_fulltext":
            return tool_result(Client().extraction_request("GET", str(args["task_id"]), "fulltext"))
        if name == "save_extraction_draft":
            return tool_result(Client().extraction_request("PUT", str(args["task_id"]), "draft",
                                                           args["draft"]))
        if name == "check_extraction_draft":
            return tool_result(Client().extraction_request("POST", str(args["task_id"]), "check", {}))
        if name == "analyze_extraction_consensus":
            client = Client()
            task_id = args.get("task_id")
            if not task_id:
                project, tasks, readiness = client.list_consensus_tasks(status="any")
                if not tasks:
                    return tool_result({
                        "status": "WAITING_FOR_REVIEWERS",
                        "project": project,
                        "readiness": readiness,
                        "message": ("No leader consensus task is available yet. The server creates one "
                                     "automatically after both assigned reviewers submit."),
                        "missingTaskId": True,
                    })
                if len(tasks) > 1:
                    return tool_result({
                        "status": "TASK_SELECTION_REQUIRED",
                        "project": project,
                        "message": "Multiple eligible papers are available; select one internally and analyze it one paper at a time.",
                        "tasks": [{key: task.get(key) for key in ("id", "paperId", "paperTitle", "status", "createdAt")} for task in tasks],
                    })
                task_id = tasks[0].get("id")
            return tool_result(client.get_extraction_consensus(str(task_id)))
        if name == "save_extraction_consensus_draft":
            client = Client()
            task_id = str(args["task_id"])
            result = client.save_extraction_consensus_draft(task_id, args["draft"])
            try:
                consensus = client.get_extraction_consensus(task_id)
            except ApiError as error:
                return tool_result({"operation": "CONSENSUS_DRAFT_SAVED", "result": result,
                                    "verified": False, "verificationError": error.as_dict()}, True)
            return tool_result({"operation": "CONSENSUS_DRAFT_SAVED", "result": result,
                                "verified": True, "consensus": consensus})
        if name == "submit_extraction_consensus":
            client = Client()
            task_id = str(args["task_id"])
            result = client.submit_extraction_consensus(task_id, args["expectedVersion"])
            try:
                consensus = client.get_extraction_consensus(task_id)
            except ApiError as error:
                return tool_result({"operation": "CONSENSUS_OFFICIAL_SUBMITTED", "official": True,
                                    "result": result, "verified": False,
                                    "verificationError": error.as_dict()}, True)
            return tool_result({"operation": "CONSENSUS_OFFICIAL_SUBMITTED", "official": True,
                                "result": result, "verified": True, "consensus": consensus})
        if name == "submit_extraction_draft":
            result = Client().submit_extraction_proposal(str(args["task_id"]))
            return tool_result({"taskId": str(args["task_id"]), "status": result.get("status", "SUBMITTED"),
                                "submission": result})
        if name == "finalize_extraction_consensus":
            result = Client().submit_extraction_proposal(str(args["task_id"]), consensus=True,
                                                          proposal=args["proposal"])
            return tool_result({"taskId": str(args["task_id"]), "status": "CONSENSUS_PROPOSAL",
                                "proposal": result})
        if name == "get_quality_context":
            return tool_result(Client().quality_request("GET", "/context"))
        if name == "list_quality_tasks":
            return tool_result(Client().quality_request("GET", "/tasks"))
        if name == "get_quality_strategy":
            return tool_result(Client().quality_request("GET", "/strategy"))
        if name == "get_quality_source":
            source = Client().get_quality_source(str(args["paper_id"]))
            # Keep the normalized Markdown as plain text so the model can copy
            # an exact quote and compute offsets against the returned source.
            return {"content": [{"type": "text", "text": source.get("markdown", "")}], "structuredContent": source}
        if name == "get_quality_assessment":
            paper_id = urllib.parse.quote(str(args["paper_id"]))
            return tool_result(Client().quality_request("GET", f"/papers/{paper_id}/assessment"))
        if name == "save_quality_assessment":
            paper_id = urllib.parse.quote(str(args["paper_id"]))
            draft = _validate_quality_assessment_draft(args.get("draft"))
            return tool_result(Client().quality_request("PUT", f"/papers/{paper_id}/assessment/draft", draft))
        if name == "check_quality_assessment":
            paper_id = urllib.parse.quote(str(args["paper_id"]))
            return tool_result(Client().quality_request("POST", f"/papers/{paper_id}/assessment/check", {}))
        if name == "submit_quality_assessment":
            return tool_result(Client().submit_quality_assessment(str(args["paper_id"])))
        if name == "analyze_quality_consensus":
            paper_id = urllib.parse.quote(str(args["paper_id"]))
            return tool_result(Client().quality_request("GET", f"/papers/{paper_id}/consensus"))
        if name == "save_quality_resolution_draft":
            paper_id = urllib.parse.quote(str(args["paper_id"]))
            return tool_result(Client().quality_request("PUT", f"/papers/{paper_id}/resolution-draft", args["draft"]))
        if name == "submit_quality_resolution":
            return tool_result(Client().submit_quality_resolution(str(args["paper_id"]), args["finalDecision"], args.get("resolutionNotes")))
        if name == "start_quality_phase":
            return tool_result(Client().quality_request("POST", "/start", {}))
        if name == "complete_quality_phase":
            return tool_result(Client().quality_request("POST", "/complete", {"confirmation": True}))
        if name == "get_markdown":
            tid = str(args["task_id"])
            try:
                text = Client().markdown(tid, int(args.get("max_wait", 30)))
            except ApiError as e:
                if e.status == 202:
                    return tool_result({"taskId": tid, "status": "pending", "message": e.message})
                if e.status == 409:
                    return tool_result({"taskId": tid, "status": "unavailable", "code": e.code,
                                        "message": e.message})
                raise
            return {"content": [{"type": "text", "text": text}],
                    "structuredContent": {"taskId": tid, "status": "ready", "chars": len(text)},
                    "isError": False}
        if name == "submit_result":
            body = build_body(args.get("decision"), args.get("reasoning"), args.get("data"))
            tid = str(args["task_id"])
            try:
                refreshed = Client().get_task(tid)
                slr.validate_task_result(refreshed, body)
                task = Client().submit(tid, body)
                mark_submitted(tid, "submitted")
                return tool_result({"taskId": tid, "status": "submitted", "decision": body["decision"],
                                    "taskStatus": (task or {}).get("status")})
            except ApiError as e:
                if e.exit_code == slr.EXIT_CONFLICT:
                    mark_submitted(tid, "already_completed")
                    return tool_result({"taskId": tid, "status": "already_completed", "message": e.message})
                raise
        if name == "save_draft":
            return tool_result(save_draft(str(args["task_id"]), args, str(args.get("title") or "")))
        if name == "list_drafts":
            return tool_result(drafts_table())
        if name == "get_draft":
            return tool_result(get_draft(str(args["task_id"])))
        if name == "submit_drafts":
            res = submit_drafts([str(i) for i in args.get("task_ids") or []] or None)
            return tool_result(res, not res["ok"])
        if name == "config_show":
            return tool_result(show_config())
        if name == "config_set_url":
            return tool_result(set_url(str(args["url"]), args.get("profile")))
        if name == "config_set_token":
            res = set_token(str(args["token"]), args.get("profile"))
            return tool_result(res, res.get("connected") is False)
        if name == "config_use":
            res = use_profile(str(args["profile"]))
            res.update(connection_test())
            return tool_result(res)
        return tool_result({"error": f"unknown tool {name}"}, True)
    except ApiError as e:
        return tool_result({**e.as_dict(), "status": "error", "httpStatus": e.status}, True)
    except Exception as e:  # never crash the server
        log(traceback.format_exc())
        return tool_result({"status": "error", "message": str(e)}, True)


def handle(msg):
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:  # notification
        return None
    if method == "initialize":
        req = (msg.get("params") or {}).get("protocolVersion")
        ver = req if req in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": ver,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": ("Connector for /slr screening, extraction and quality assessment. Screening results are "
                             "advisory recommendations and never human votes. Always render "
                             "complete inline Markdown tables for review results: task metadata, "
                             "every custom extraction field, every evidence item, and every "
                             "validation check, one paper at a time. Never combine multiple "
                             "papers/tasks in one table. Never render standalone source "
                             "asset/revision/hash, template-schema, or aggregate-check tables; "
                             "keep only relevant details inside the current paper's tables. "
                             "Reviewer-scoped "
                             "extraction submissions write canonical reviewer data after explicit "
                             "user approval. Leader consensus tools require the leader capability, "
                             "both reviewer submissions, and compare every custom field plus every "
                             "evidence item. Missing reviewers and CONFLICT stop the official submit; "
                             "only a conflict-free draft may finalize canonical project consensus. "
                             "Quality Assessment commands always render complete criterion-level tables "
                             "with reviewer values, scores, comments, evidence quotes, page/section "
                             "locations, checks and conflict reasons. Source asset IDs, source revisions, "
                             "source hashes, quote hashes, exact offsets, and pdfHighlightCoordinates "
                             "remain internal to the MCP payload and are not rendered. In reviewer "
                             "scope, omit the Reviewer column because the current reviewer is implicit. "
                             "For QA saves, draft must contain decisionItems (one per configured "
                             "criterion) with numeric value 0/1/2 and source-bound evidence; never send "
                             "legacy fields, criteria, values, assessment, or an empty probe draft. A save "
                             "error means no draft exists, so do not call QA check and report "
                             "QA_DRAFT_NOT_FOUND as if it were the primary failure. Reviewer tokens "
                             "never receive peer content. Leader QA comparisons cover every criterion and official "
                             "resolution requires explicit confirmation after both reviewer assessments "
                             "are complete. submit_drafts / submit_result and all extraction/QA writes require explicit "
                             "user approval. Never repeat the agent token."),
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        p = msg.get("params") or {}
        return {"jsonrpc": "2.0", "id": mid, "result": call_tool(p.get("name"), p.get("arguments") or {})}
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"Method not found: {method}"}}


def main():
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", newline="\n")
        except Exception:
            pass
    log(f"started (profile {active_profile()})")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            msgs = msg if isinstance(msg, list) else [msg]
            out = [r for r in (handle(m) for m in msgs) if r is not None]
            if not out:
                continue
            resp = out if isinstance(msg, list) else out[0]
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
