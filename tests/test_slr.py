"""End-to-end checks: slr_mcp.py over stdio against a fake Agent Task API.

Run: python -m unittest discover -s tests -v   (stdlib only, no network)
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
TOKEN = "slrp_test_token_1234567890"
# Same shape as the Claude Science skill (SKILL.md 'Vote format').
DATA = {"basis": "title_abstract", "confidence": 0.9, "studyDesign": "RCT", "population": "Adults",
        "intervention": None, "comparator": None, "outcomes": [], "exclusionTriggered": [],
        "evidence": ["Abstract: adults aged 18-65"],
        "criteria": [{"id": "IC1", "text": "Adults", "status": "met", "evidence": "adults aged 18-65 (Abstract)"},
                     {"id": "EC1", "text": "Reviews", "status": "not_met", "evidence": "primary study (Abstract)"}]}
# A vote the Claude Science skill pushed (abridged); must be accepted unchanged.
CLAUDE_VOTE = {
    "decision": "INCLUDE",
    "reasoning": "Decision: INCLUDE\nBasis: title + abstract only\n\nKey reason: K-asynchronous aggregation.",
    "data": {
        "comparator": "Existing FL-based IDS methods (not named in the abstract)",
        "evidence": ["Abstract: aggregation eligibility and order based on gradient quality and staleness"],
        "exclusionTriggered": [], "outcomes": ["~92% accuracy CICIDS2017"],
        "criteria": [
            {"evidence": "Proposed scheme with three-dataset evaluation (Abstract)", "id": "IC1",
             "text": "Primary study", "status": "met"},
            {"evidence": "Node selection for global aggregation of client-uploaded gradients (Abstract)",
             "id": "IC2", "text": "FL or server-based training with a central aggregator", "status": "met"}],
        "confidence": 0.88, "studyDesign": "Scheme proposal", "basis": "title_abstract",
        "intervention": "DWKAFL", "population": "Smart-grid devices as FL clients"}}


def _task(tid, stage, status="ASSIGNED"):
    t = {"id": tid, "stage": stage, "taskKind": "screening", "status": status, "paperTitle": f"Paper {tid}",
         "instructions": "Assess criteria.",
         "protocolSnapshot": {"inclusionCriteria": ["Adults"], "exclusionCriteria": ["Reviews"]},
         "paper": {"id": f"p-{tid}", "title": f"Paper {tid}", "abstract": "...", "fullTextStatus": "COMPLETED"}}
    if stage == "FULL_TEXT":
        t["markdownPath"] = f"/api/agent/v1/tasks/{tid}/markdown"
    return t


class FakeApi(BaseHTTPRequestHandler):
    tasks = {}
    posts = []
    extraction_drafts = {}
    extraction_posts = []
    list_stages = []
    check_status = "PASS"
    inactive = set()

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", headers=None):
        raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _auth(self):
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._send(401, {"success": False, "error": {"code": "UNAUTHORIZED", "message": "bad token"}})
            return False
        return True

    def do_GET(self):
        if not self._auth():
            return
        path, _, query = self.path.partition("?")
        ids = sorted(self.tasks)
        if path == "/api/agent/v1/tasks":
            params = dict(part.split("=", 1) for part in query.split("&") if "=" in part)
            stage = params.get("stage")
            self.list_stages.append(stage)
            if stage:
                ids = [tid for tid in ids if self.tasks[tid].get("stage") == stage
                       and self.tasks[tid].get("taskKind") == "screening"]
            # one item per page, to exercise the cursor loop; cursor = "c<offset>"
            start = int(query.split("cursor=c")[1].split("&")[0]) if "cursor=c" in query else 0
            items = [self.tasks[i] for i in ids[start:start + 1]]
            more = start + 1 < len(ids)
            return self._send(200, {"success": True, "data": {
                "project": {"name": "Demo review"}, "items": items,
                "nextCursor": f"c{start + 1}" if more else None, "hasMore": more}})
        if path.startswith("/api/agent/v1/extraction/tasks/"):
            parts = path.strip("/").split("/")
            tid, action = parts[5], parts[6]
            if tid not in self.tasks:
                return self._send(404, {"success": False, "error": {"code": "NOT_FOUND", "message": "no task"}})
            if action == "context":
                return self._send(200, {"success": True, "data": {"task": self.tasks[tid],
                                 "template": {"version": "v1"}, "draft": self.extraction_drafts.get(tid)}})
            if action == "fulltext":
                return self._send(200, {"success": True, "data": {"markdown": "Adults 18-65.",
                                 "sourceAssetId": "asset-1", "sourceRevision": 3,
                                 "sourceHash": "sha256:abc", "paragraphs": [{"startOffset": 0, "endOffset": 13}]}})
        parts = path.strip("/").split("/")  # api agent v1 tasks <id> [markdown]
        tid = parts[4] if len(parts) > 4 else None
        if tid not in self.tasks:
            return self._send(404, {"success": False, "error": {"code": "NOT_FOUND", "message": "no task"}})
        if len(parts) == 5:
            return self._send(200, {"success": True, "data": {"task": self.tasks[tid]}})
        if tid == "t-pending":
            return self._send(202, {"success": True, "data": {"status": "PROCESSING"}}, headers={"Retry-After": "10"})
        if tid == "t-error502":
            return self._send(502, {"success": False, "error": {"code": "ASSET_READ_FAILED", "message": "asset unavailable"}})
        return self._send(200, "# Methods\nAdults 18-65.", "text/markdown; charset=utf-8")

    def do_POST(self):
        if not self._auth():
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        path = self.path.split("?", 1)[0]
        if "/extraction/tasks/" in path:
            parts = path.strip("/").split("/")
            tid, action = parts[5], parts[6]
            self.extraction_posts.append((tid, action, body))
            if action == "check":
                check = {"version": 1, "status": self.check_status}
                if self.check_status == "BLOCK":
                    check = {"check": check}
                return self._send(200, {"success": True, "data": check})
            if action in ("submit-challenge", "consensus-challenge"):
                return self._send(200, {"success": True, "data": {
                    "challengeId": f"challenge-{action}",
                    "payloadDigest": "d" * 64,
                }})
            if action in ("submit", "consensus-proposal"):
                if not body.get("challengeId"):
                    return self._send(400, {"success": False, "error": {"code": "CHALLENGE_REQUIRED", "message": "challenge required"}})
                return self._send(200, {"success": True, "data": {"status": "PENDING_HUMAN_PROMOTION", "proposalId": "proposal-1"}})
            return self._send(404, {"success": False, "error": {"code": "NOT_FOUND", "message": "unknown extraction action"}})
        tid = path.strip("/").split("/")[4]
        if self.tasks[tid]["status"] == "COMPLETED":
            return self._send(409, {"success": False, "error": {"code": "TASK_COMPLETED", "message": "done"}})
        if tid in self.inactive:
            return self._send(409, {"success": False, "error": {"code": "TASK_STAGE_INACTIVE", "message": "stage moved"}})
        self.posts.append((tid, body))
        self.tasks[tid]["status"] = "COMPLETED"
        return self._send(200, {"success": True, "data": self.tasks[tid]})

    def do_PUT(self):
        if not self._auth():
            return
        path = self.path.split("?", 1)[0]
        parts = path.strip("/").split("/")
        tid, action = parts[5], parts[6]
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.extraction_drafts[tid] = body
        self.extraction_posts.append((tid, action, body))
        return self._send(200, {"success": True, "data": {"draft": body}})


class McpEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeApi)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        FakeApi.tasks = {t["id"]: t for t in (_task("t-ta", "TITLE_ABSTRACT"), _task("t-ft", "FULL_TEXT"),
                                              _task("t-pending", "FULL_TEXT"), _task("t-extract", "FULL_TEXT"),
                                              _task("t-ta2", "TITLE_ABSTRACT"), _task("t-error502", "FULL_TEXT"))}
        FakeApi.tasks["t-extract"]["taskKind"] = "extraction"
        FakeApi.posts = []
        FakeApi.extraction_posts = []
        FakeApi.extraction_drafts = {}
        FakeApi.list_stages = []
        FakeApi.check_status = "PASS"
        FakeApi.inactive = set()
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("API_BASE_URL", "AGENT_TOKEN", "AGENT_TOKEN_FILE", "REVIEW_PROFILE")}
        self.env.update(REVIEW_CONFIG_DIR=self.tmp.name, REVIEW_WORKDIR=str(Path(self.tmp.name) / "pr"),
                        PYTHONIOENCODING="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def mcp(self, *calls):
        """Run one MCP session: initialize, then each (tool, args); return tool results."""
        msgs = [{"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"}]
        msgs += [{"jsonrpc": "2.0", "id": i + 1, "method": "tools/call", "params": {"name": n, "arguments": a}}
                 for i, (n, a) in enumerate(calls)]
        p = subprocess.run([sys.executable, str(SCRIPTS / "slr_mcp.py")], env=self.env, timeout=60,
                           input="\n".join(json.dumps(m) for m in msgs) + "\n",
                           capture_output=True, text=True, encoding="utf-8")
        out = {m["id"]: m for m in map(json.loads, p.stdout.splitlines())}
        self.assertEqual(out[0]["result"]["serverInfo"]["name"], "slr")
        return [out[i + 1]["result"] for i in range(len(calls))]

    def configure(self):
        url, tok = self.mcp(("config_set_url", {"url": self.url}), ("config_set_token", {"token": TOKEN}))
        return url, tok

    def test_tools_list(self):
        p = subprocess.run([sys.executable, str(SCRIPTS / "slr_mcp.py")], env=self.env, timeout=60,
                           input='{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n',
                           capture_output=True, text=True, encoding="utf-8")
        tools = json.loads(p.stdout)["result"]["tools"]
        names = {t["name"] for t in tools}
        self.assertEqual(names, {"connection_test", "list_tasks", "get_task", "get_markdown", "submit_result",
                                 "list_consensus_tasks",
                                 "get_extraction_context", "get_extraction_fulltext", "save_extraction_draft",
                                 "check_extraction_draft", "submit_extraction_draft", "finalize_extraction_consensus",
                                 "analyze_extraction_consensus", "save_extraction_consensus_draft",
                                 "submit_extraction_consensus",
                                 "get_quality_context", "list_quality_tasks", "get_quality_strategy",
                                 "get_quality_source", "get_quality_assessment", "save_quality_assessment",
                                 "check_quality_assessment", "submit_quality_assessment",
                                 "analyze_quality_consensus", "save_quality_resolution_draft",
                                 "submit_quality_resolution", "start_quality_phase", "complete_quality_phase",
                                 "save_draft", "list_drafts", "get_draft", "submit_drafts",
                                 "config_show", "config_set_url", "config_set_token", "config_use"})
        extraction = next(t for t in tools if t["name"] == "save_extraction_draft")
        draft = extraction["inputSchema"]["properties"]["draft"]
        self.assertEqual(draft["required"], ["templateId", "templateVersion", "sourceAssetId", "sourceRevision",
                                              "sourceHash", "expectedVersion", "values", "evidence"])
        self.assertNotIn("fields", draft["properties"])

    def test_unconfigured_reports_config_error(self):
        (res,) = self.mcp(("connection_test", {}))
        self.assertTrue(res["isError"])
        self.assertEqual(res["structuredContent"]["error"]["code"], "CONFIG")

    def test_config_from_chat_masks_token(self):
        url, tok = self.configure()
        self.assertEqual(url["structuredContent"]["api_base_url"], self.url + "/api")
        self.assertTrue(tok["structuredContent"]["connected"])
        self.assertNotIn(TOKEN, json.dumps(tok))
        (show,) = self.mcp(("config_show", {}))
        self.assertNotIn(TOKEN, json.dumps(show))
        self.assertTrue((Path(self.tmp.name) / "tokens" / "default").exists())

    def test_bad_token_is_error(self):
        self.mcp(("config_set_url", {"url": self.url}))
        (res,) = self.mcp(("config_set_token", {"token": "slrp_wrong_token_000000"}))
        self.assertTrue(res["isError"])
        self.assertEqual(res["structuredContent"]["error"]["status"], 401)

    def test_list_follows_cursor_and_counts(self):
        self.configure()
        ct, lst = self.mcp(("connection_test", {}), ("list_tasks", {}))
        self.assertEqual(ct["structuredContent"]["assigned"], 6)
        self.assertEqual(ct["structuredContent"]["project"], "Demo review")
        self.assertEqual(lst["structuredContent"]["count"], 6)
        rendered = lst["content"][0]["text"]
        self.assertIn("| id |", rendered)
        self.assertIn("t-ta", rendered)
        self.assertNotIn('"items":', rendered)

    def test_stage_filter_is_sent_on_each_cursor_page(self):
        self.configure()
        FakeApi.list_stages = []
        (listed,) = self.mcp(("list_tasks", {"stage": "TITLE_ABSTRACT"}),)
        self.assertEqual(listed["structuredContent"]["count"], 2)
        self.assertTrue(FakeApi.list_stages)
        self.assertEqual(set(FakeApi.list_stages), {"TITLE_ABSTRACT"})

    def test_markdown_stage_guard_pending_and_ready(self):
        self.configure()
        ta, pend, ft = self.mcp(("get_markdown", {"task_id": "t-ta"}),
                                ("get_markdown", {"task_id": "t-pending", "max_wait": 0}),
                                ("get_markdown", {"task_id": "t-ft"}))
        self.assertEqual(ta["structuredContent"]["status"], "unavailable")
        self.assertEqual(ta["structuredContent"]["code"], "FULLTEXT_STAGE_REQUIRED")
        self.assertEqual(pend["structuredContent"]["status"], "pending")
        self.assertEqual(ft["structuredContent"]["status"], "ready")
        self.assertIn("Adults 18-65", ft["content"][0]["text"])

    def test_not_found_and_asset_gateway_errors_are_preserved(self):
        self.configure()
        missing, gateway = self.mcp(("get_task", {"task_id": "missing"}),
                                    ("get_markdown", {"task_id": "t-error502"}))
        self.assertTrue(missing["isError"])
        self.assertEqual(missing["structuredContent"]["httpStatus"], 404)
        self.assertTrue(gateway["isError"])
        self.assertEqual(gateway["structuredContent"]["httpStatus"], 502)

    def test_submit_once_then_already_completed(self):
        self.configure()
        vote = {"task_id": "t-ta", "decision": "INCLUDE", "reasoning": "Key reason: adults.", "data": DATA}
        first, second, task = self.mcp(("submit_result", vote), ("submit_result", vote),
                                       ("get_task", {"task_id": "t-ta"}))
        self.assertEqual(first["structuredContent"]["status"], "submitted")
        self.assertEqual(second["structuredContent"]["status"], "already_completed")
        self.assertEqual(task["structuredContent"]["task"]["status"], "COMPLETED")
        self.assertEqual(len(FakeApi.posts), 1)

    def test_stage_inactive_conflict_is_not_reported_as_completed(self):
        self.configure()
        FakeApi.inactive = {"t-ta"}
        (res,) = self.mcp(("submit_result", {"task_id": "t-ta", "decision": "EXCLUDE",
                                               "reasoning": "rationale", "data": DATA}))
        self.assertTrue(res["isError"])
        self.assertEqual(res["structuredContent"]["code"], "TASK_STAGE_INACTIVE")
        self.assertEqual(FakeApi.tasks["t-ta"]["status"], "ASSIGNED")

    def test_submit_validation_blocks_post(self):
        self.configure()
        (res,) = self.mcp(("submit_result", {"task_id": "t-ta", "decision": "INCLUDE", "reasoning": "  ",
                                             "data": DATA}))
        self.assertTrue(res["isError"])
        self.assertEqual(FakeApi.posts, [])

    def test_claude_format_vote_is_accepted(self):
        self.configure()
        (res,) = self.mcp(("submit_result", {"task_id": "t-ta", **CLAUDE_VOTE}))
        self.assertEqual(res["structuredContent"]["status"], "submitted")
        self.assertEqual(FakeApi.posts, [("t-ta", {"decision": "ADVANCE_TO_FULL_TEXT",
                                                   "reasoning": CLAUDE_VOTE["reasoning"],
                                                   "data": CLAUDE_VOTE["data"]})])

    def test_stage_decision_rules_and_advisory_alias(self):
        self.configure()
        bad, good, fulltext_bad = self.mcp(
            ("submit_result", {"task_id": "t-ta", "decision": "INCLUDE", "reasoning": "x", "data": DATA}),
            ("submit_result", {"task_id": "t-ta", "decision": "ADVANCE_TO_FULL_TEXT", "reasoning": "x", "data": DATA}),
            ("submit_result", {"task_id": "t-ft", "decision": "ADVANCE_TO_FULL_TEXT", "reasoning": "x", "data": DATA}))
        self.assertEqual(bad["structuredContent"]["status"], "submitted")
        self.assertEqual(good["structuredContent"]["status"], "already_completed")
        self.assertTrue(fulltext_bad["isError"])
        self.assertEqual(FakeApi.posts[0][1]["decision"], "ADVANCE_TO_FULL_TEXT")
        self.assertEqual(len(FakeApi.posts), 1)

    def test_screening_submit_refuses_extraction_task(self):
        self.configure()
        (res,) = self.mcp(("submit_result", {"task_id": "t-extract", "decision": "INCLUDE",
                                               "reasoning": "rationale", "data": DATA}))
        self.assertTrue(res["isError"])
        self.assertEqual(res["structuredContent"]["code"], "EXTRACTION_TASK_REQUIRED")
        self.assertEqual(FakeApi.posts, [])

    def test_missing_task_kind_is_never_assumed_to_be_screening(self):
        self.configure()
        unknown = _task("t-unknown-kind", "TITLE_ABSTRACT")
        unknown.pop("taskKind")
        FakeApi.tasks[unknown["id"]] = unknown
        (res,) = self.mcp(("submit_result", {"task_id": unknown["id"], "decision": "EXCLUDE",
                                               "reasoning": "rationale", "data": DATA}))
        self.assertTrue(res["isError"])
        self.assertEqual(res["structuredContent"]["code"], "TASK_KIND_REQUIRED")
        self.assertEqual(FakeApi.posts, [])

    def test_extraction_tools_and_challenge_handshake(self):
        self.configure()
        draft = {"templateId": "template-1", "templateVersion": 1, "sourceAssetId": "asset-1",
                 "sourceRevision": "3", "sourceHash": "a" * 64, "expectedVersion": 0,
                 "values": {"f1": {"stringValue": "Adults"}}, "evidence": []}
        context, text, saved, checked, submitted = self.mcp(
            ("get_extraction_context", {"task_id": "t-extract"}),
            ("get_extraction_fulltext", {"task_id": "t-extract"}),
            ("save_extraction_draft", {"task_id": "t-extract", "draft": draft}),
            ("check_extraction_draft", {"task_id": "t-extract"}),
            ("submit_extraction_draft", {"task_id": "t-extract"}))
        self.assertEqual(context["structuredContent"]["template"]["version"], "v1")
        self.assertEqual(text["structuredContent"]["sourceHash"], "sha256:abc")
        self.assertEqual(saved["structuredContent"]["draft"], draft)
        self.assertEqual(checked["structuredContent"]["status"], "PASS")
        self.assertEqual(submitted["structuredContent"]["status"], "PENDING_HUMAN_PROMOTION")
        submit_post = next(entry for entry in FakeApi.extraction_posts if entry[1] == "submit")
        self.assertEqual(submit_post[2], {
            "challengeId": "challenge-submit-challenge",
            "payloadDigest": "d" * 64,
            "idempotencyKey": "mcp-extraction-direct-v2-t-extract-" + "d" * 64,
        })

        proposal = {"expectedVersion": 0, "values": {"f1": {"stringValue": "Adults"}}, "findings": []}
        (consensus,) = self.mcp(("finalize_extraction_consensus", {"task_id": "t-extract",
                                                                     "proposal": proposal}))
        self.assertEqual(consensus["structuredContent"]["status"], "CONSENSUS_PROPOSAL")
        challenge_post = next(entry for entry in FakeApi.extraction_posts if entry[1] == "consensus-challenge")
        self.assertEqual(challenge_post[2], proposal)
        consensus_post = next(entry for entry in FakeApi.extraction_posts if entry[1] == "consensus-proposal")
        self.assertEqual(consensus_post[2], {
            "challengeId": "challenge-consensus-challenge",
            "payloadDigest": "d" * 64,
            "idempotencyKey": "mcp-consensus-t-extract-" + "d" * 64,
        })

    def test_quality_assessment_draft_save_url_encodes_paper_id(self):
        """Regression: Phase 3 QA handlers must import urllib before quoting paper ids."""
        self.configure()
        draft = {"notes": None, "decisionItems": [{"qualityCriterionId": "C1", "value": 1,
                                                       "comment": "not applicable", "evidence": []}]}
        (saved,) = self.mcp(("save_quality_assessment", {"paper_id": "paper/1", "draft": draft}),)
        self.assertFalse(saved["isError"])
        self.assertEqual(saved["structuredContent"]["draft"], draft)

    def test_quality_assessment_rejects_legacy_or_empty_drafts_before_http(self):
        """A malformed draft must not become the misleading QA_DRAFT_NOT_FOUND follow-up."""
        self.configure()
        legacy, empty = self.mcp(
            ("save_quality_assessment", {"paper_id": "paper/1", "draft": {"fields": []}}),
            ("save_quality_assessment", {"paper_id": "paper/1", "draft": {"decisionItems": []}}),
        )
        for result in (legacy, empty):
            self.assertTrue(result["isError"])
            self.assertEqual(result["structuredContent"]["code"], "INVALID_QA_DRAFT")
        self.assertFalse(FakeApi.extraction_posts)

    def test_blocked_extraction_draft_never_challenges(self):
        self.configure()
        FakeApi.check_status = "BLOCK"
        (res,) = self.mcp(("submit_extraction_draft", {"task_id": "t-extract"}))
        self.assertTrue(res["isError"])
        self.assertEqual(res["structuredContent"]["code"], "EXTRACTION_CHECK_BLOCKED")
        self.assertFalse(any(action == "submit-challenge" for _, action, _ in FakeApi.extraction_posts))

    def test_submit_without_criteria_is_rejected(self):
        # Regression: votes with data = {confidence, stage} showed no "Criteria assessment" in the web app.
        self.configure()
        base = {"task_id": "t-ta", "decision": "INCLUDE", "reasoning": "I1: met. I2: met."}
        bad = [None, {"confidence": 0.9, "stage": "TITLE_ABSTRACT"},
               {**DATA, "criteria": []},
               {**DATA, "criteria": [{"id": "IC1", "text": "Adults", "status": "met"}]},
               {**DATA, "criteria": [{"id": "IC1", "status": "met", "evidence": "x (Abstract)"}]},
               {**DATA, "criteria": [{**DATA["criteria"][0], "status": "yes"}]},
               {**DATA, "basis": "abstract"}]
        results = self.mcp(*[("submit_result", {**base, **({} if d is None else {"data": d})}) for d in bad])
        for d, res in zip(bad, results):
            self.assertTrue(res["isError"], d)
            self.assertEqual(res["structuredContent"]["code"], "VALIDATION", d)
        self.assertEqual(FakeApi.posts, [])

    def test_profiles_switch_live(self):
        self.configure()
        self.mcp(("config_set_url", {"url": "http://127.0.0.1:9/api", "profile": "other"}))
        (sw,) = self.mcp(("config_use", {"profile": "other"}))
        self.assertEqual(sw["structuredContent"]["current"], "other")
        self.assertFalse(sw["structuredContent"]["connected"])
        (back,) = self.mcp(("config_use", {"profile": "default"}))
        self.assertTrue(back["structuredContent"]["connected"])

    def test_mcp_draft_flow_without_shell(self):
        # Open-Science agents cannot run the skill's scripts: drafts go through the connector.
        self.configure()
        vote = {"task_id": "t-ta", "title": "Paper t-ta", "decision": "INCLUDE",
                "reasoning": "Decision: INCLUDE\nKey reason: adults.", "data": DATA}
        bad, saved, edited, table, one = self.mcp(
            ("save_draft", {**vote, "data": {"confidence": 0.9, "stage": "TITLE_ABSTRACT"}}),
            ("save_draft", vote),
            ("save_draft", {**vote, "decision": "UNCERTAIN"}),
            ("list_drafts", {}),
            ("get_draft", {"task_id": "t-ta"}))
        self.assertEqual(bad["structuredContent"]["code"], "VALIDATION")
        self.assertFalse(saved["structuredContent"]["replaced"])
        self.assertTrue(edited["structuredContent"]["replaced"])
        t = table["structuredContent"]
        self.assertEqual((t["pending"], t["drafts"][0]["keyReason"]), (1, "adults."))
        self.assertEqual(one["structuredContent"]["body"]["data"], DATA)
        self.assertEqual(FakeApi.posts, [])  # saving never submits

        sent, again, after = self.mcp(("submit_drafts", {"task_ids": ["t-ta"]}),
                                      ("submit_drafts", {"task_ids": ["t-ta"]}),
                                      ("list_drafts", {}))
        self.assertEqual(sent["structuredContent"]["results"][0]["result"], "submitted")
        self.assertEqual(FakeApi.posts, [("t-ta", {"decision": "UNCERTAIN", "reasoning": vote["reasoning"],
                                                   "data": DATA})])
        self.assertTrue(again["isError"])
        self.assertEqual(again["structuredContent"]["noDraft"], ["t-ta"])
        self.assertEqual((after["structuredContent"]["pending"], after["structuredContent"]["submittedIds"]),
                         (0, ["t-ta"]))
        audit = (Path(self.env["REVIEW_WORKDIR"]) / "audit.jsonl").read_text(encoding="utf-8")
        self.assertEqual([json.loads(l)["event"] for l in audit.splitlines()], ["draft", "edit", "submit"])

    def test_cli_draft_and_mark_submitted(self):
        def cli(*args):
            p = subprocess.run([sys.executable, str(SCRIPTS / "slr.py"), *args], env=self.env, timeout=60,
                               capture_output=True, text=True, encoding="utf-8")
            return json.loads(p.stdout)
        data_file = Path(self.tmp.name) / "t-ta.data.json"
        data_file.write_text(json.dumps(DATA), encoding="utf-8")
        d = cli("draft", "t-ta", "--title", "T", "--decision", "EXCLUDE",
                "--reasoning", "Decision: EXCLUDE\nKey reason: children.", "--data-file", str(data_file))
        self.assertTrue(d["ok"])
        self.assertEqual(cli("draft", "t-ft", "--decision", "INCLUDE", "--reasoning", "x",
                             "--data", '{"confidence": 0.8}')["code"], "VALIDATION")
        table = cli("drafts")
        self.assertEqual(table["drafts"][0]["keyReason"], "children.")
        m = cli("mark-submitted", "t-ta", "--result", "submitted")
        self.assertTrue(m["draftMoved"])
        self.assertEqual(cli("drafts")["pending"], 0)


if __name__ == "__main__":
    unittest.main()
