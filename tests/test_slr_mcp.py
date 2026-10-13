"""MCP integration checks against an in-process fake Agent Task API."""
import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MCP = ROOT / "scripts" / "slr_mcp.py"
TOKEN = "slrp_claude_test_token_123456"


def task(task_id, stage, kind="screening", status="ASSIGNED"):
    item = {"id": task_id, "stage": stage, "taskKind": kind, "status": status,
            "paperTitle": task_id, "paper": {"title": task_id, "abstract": "abstract"}}
    if stage == "FULL_TEXT":
        item["markdownPath"] = f"/api/agent/v1/tasks/{task_id}/markdown"
    return item


class FakeApi(BaseHTTPRequestHandler):
    tasks = {}
    posts = []
    inactive = set()
    list_stages = []

    def log_message(self, *_):
        pass

    def send_json(self, status, payload):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def authorized(self):
        if self.headers.get("Authorization") == f"Bearer {TOKEN}":
            return True
        self.send_json(401, {"success": False, "error": {"code": "UNAUTHORIZED", "message": "bad token"}})
        return False

    def do_GET(self):
        if not self.authorized():
            return
        path, _, query = self.path.partition("?")
        if path == "/api/agent/v1/tasks":
            params = dict(part.split("=", 1) for part in query.split("&") if "=" in part)
            stage = params.get("stage")
            self.list_stages.append(stage)
            ids = sorted(tid for tid, item in self.tasks.items()
                         if not stage or (item.get("stage") == stage and item.get("taskKind") == "screening"))
            cursor = int(params.get("cursor", "0").removeprefix("c"))
            page = [self.tasks[tid] for tid in ids[cursor:cursor + 1]]
            more = cursor + 1 < len(ids)
            self.send_json(200, {"success": True, "data": {"project": {"name": "Demo"},
                           "items": page, "nextCursor": f"c{cursor + 1}" if more else None,
                           "hasMore": more}})
            return
        parts = path.strip("/").split("/")
        tid = parts[4] if len(parts) > 4 else ""
        if tid not in self.tasks:
            self.send_json(404, {"success": False, "error": {"code": "NOT_FOUND", "message": "missing"}})
            return
        if len(parts) == 5:
            self.send_json(200, {"success": True, "data": {"task": self.tasks[tid]}})
        elif parts[5] == "markdown":
            if tid == "badasset":
                return self.send_json(502, {"success": False,
                                            "error": {"code": "ASSET_READ_FAILED", "message": "asset unavailable"}})
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"# Methods\nAdults aged 18-65.")

    def do_POST(self):
        if not self.authorized():
            return
        path = self.path.split("?", 1)[0]
        tid = path.strip("/").split("/")[4]
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if tid not in self.tasks:
            return self.send_json(404, {"success": False, "error": {"code": "NOT_FOUND", "message": "missing"}})
        if tid in self.inactive:
            return self.send_json(409, {"success": False, "error": {"code": "TASK_STAGE_INACTIVE", "message": "stage moved"}})
        if self.tasks[tid]["status"] == "COMPLETED":
            return self.send_json(409, {"success": False, "error": {"code": "TASK_COMPLETED", "message": "done"}})
        self.posts.append((tid, body))
        self.tasks[tid]["status"] = "COMPLETED"
        self.send_json(200, {"success": True, "data": self.tasks[tid]})


class ClaudeMcpIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeApi)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}/api"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeApi.tasks = {"ta1": task("ta1", "TITLE_ABSTRACT"),
                         "ta2": task("ta2", "TITLE_ABSTRACT"),
                         "ft": task("ft", "FULL_TEXT"),
                         "extract": task("extract", "FULL_TEXT", kind="extraction"),
                         "done": task("done", "FULL_TEXT", status="COMPLETED"),
                         "inactive": task("inactive", "FULL_TEXT"),
                         "badasset": task("badasset", "FULL_TEXT"),
                         "unknownkind": task("unknownkind", "TITLE_ABSTRACT", kind=None)}
        FakeApi.posts = []
        FakeApi.inactive = {"inactive"}
        FakeApi.list_stages = []
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("API_BASE_URL", "AGENT_TOKEN", "AGENT_TOKEN_FILE", "REVIEW_PROFILE")}
        self.env.update(API_BASE_URL=self.base, AGENT_TOKEN=TOKEN, PYTHONIOENCODING="utf-8")

    def mcp(self, *calls):
        messages = [{"jsonrpc": "2.0", "id": 0, "method": "initialize",
                     "params": {"protocolVersion": "2025-06-18"}}]
        messages.extend({"jsonrpc": "2.0", "id": i + 1, "method": "tools/call",
                         "params": {"name": name, "arguments": args}}
                        for i, (name, args) in enumerate(calls))
        proc = subprocess.run([sys.executable, str(MCP)], env=self.env, timeout=30,
                              input="\n".join(map(json.dumps, messages)) + "\n",
                              capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        replies = [json.loads(line) for line in proc.stdout.splitlines()]
        return [next(reply["result"] for reply in replies if reply.get("id") == i + 1)
                for i in range(len(calls))]

    def test_stage_filter_kept_through_cursor_pages(self):
        FakeApi.list_stages = []
        (result,) = self.mcp(("list_tasks", {"stage": "TITLE_ABSTRACT"}),)
        self.assertEqual(result["structuredContent"]["count"], 2)
        self.assertEqual(FakeApi.list_stages, ["TITLE_ABSTRACT", "TITLE_ABSTRACT"])

        FakeApi.list_stages = []
        proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "slr.py"), "list", "--all",
                               "--stage", "TITLE_ABSTRACT"], env=self.env, timeout=30,
                              capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["count"], 2)
        self.assertEqual(FakeApi.list_stages, ["TITLE_ABSTRACT", "TITLE_ABSTRACT"])

    def test_stage_recommendation_alias_and_invalid_combinations(self):
        calls = (
            ("submit_result", {"task_id": "ta1", "decision": "include", "reasoning": "ok"}),
            ("submit_result", {"task_id": "ft", "decision": "ADVANCE_TO_FULL_TEXT", "reasoning": "bad"}),
            ("submit_result", {"task_id": "extract", "decision": "INCLUDE", "reasoning": "bad"}),
        )
        alias, invalid_stage, wrong_kind = self.mcp(*calls)
        self.assertEqual(alias["structuredContent"]["decision"], "ADVANCE_TO_FULL_TEXT")
        self.assertEqual(FakeApi.posts[0][1]["decision"], "ADVANCE_TO_FULL_TEXT")
        self.assertTrue(invalid_stage["isError"])
        self.assertEqual(invalid_stage["structuredContent"]["code"], "VALIDATION")
        self.assertTrue(wrong_kind["isError"])
        self.assertEqual(wrong_kind["structuredContent"]["code"], "EXTRACTION_TASK_REQUIRED")
        self.assertEqual(len(FakeApi.posts), 1)

    def test_missing_task_kind_is_never_assumed_to_be_screening(self):
        result, = self.mcp(("submit_result", {"task_id": "unknownkind", "decision": "EXCLUDE",
                                               "reasoning": "rationale"}),)
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["code"], "TASK_KIND_REQUIRED")
        self.assertEqual(FakeApi.posts, [])

    def test_completed_409_is_distinguished_from_inactive_stage_409(self):
        done, inactive = self.mcp(
            ("submit_result", {"task_id": "done", "decision": "INCLUDE", "reasoning": "done"}),
            ("submit_result", {"task_id": "inactive", "decision": "INCLUDE", "reasoning": "inactive"}))
        self.assertEqual(done["structuredContent"]["status"], "already_completed")
        self.assertTrue(inactive["isError"])
        self.assertEqual(inactive["structuredContent"]["code"], "TASK_STAGE_INACTIVE")

    def test_markdown_and_tool_surface(self):
        bad, good = self.mcp(("get_markdown", {"task_id": "ta1"}),
                             ("get_markdown", {"task_id": "ft"}))
        self.assertEqual(bad["structuredContent"]["message"], "Markdown is available only for FULL_TEXT tasks.")
        self.assertIn("Adults aged 18-65", good["content"][0]["text"])
        (listed,) = self.mcp(("connection_test", {}),)
        self.assertTrue(listed["structuredContent"]["connected"])

    def test_not_found_and_gateway_error_are_reported(self):
        missing, gateway = self.mcp(("get_task", {"task_id": "missing"}),
                                    ("get_markdown", {"task_id": "badasset"}))
        self.assertTrue(missing["isError"])
        self.assertEqual(missing["structuredContent"]["httpStatus"], 404)
        self.assertTrue(gateway["isError"])
        self.assertEqual(gateway["structuredContent"]["httpStatus"], 502)


if __name__ == "__main__":
    unittest.main()
