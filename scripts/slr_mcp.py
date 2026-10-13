#!/usr/bin/env python3
"""MCP server (stdio) for the SLR Agent Task API — the WRITE side of /slr.

Runs on the host, launched by Claude Science as a connector, so it reaches the
backend directly (e.g. http://localhost:5000/api) without the sandbox proxy.
Reads use the reviewer-scoped Agent Task API through the skill's slr.py client; this
server submits recommendations and exposes task status/context.

Stdlib only. Config comes from the connector's env (API_BASE_URL, AGENT_TOKEN)
or from slr.py's stored profile (~/.config/slr).

Tools:
  list_tasks(status?)                                 tasks + project criteria (read fallback)
  get_markdown(task_id, max_wait?)                    full text (read fallback)
  submit_result(task_id, decision, reasoning, data?)  POST a vote (final)
  get_task(task_id)                                   task status / result
  connection_test()                                   check URL + token
"""
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from slr import ApiError, Client, build_body  # noqa: E402

SERVER_INFO = {"name": "slr", "version": "1.0.0"}
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

TOOLS = [
    {
        "name": "submit_result",
        "title": "Submit SLR screening vote",
        "description": (
            "Submit the final screening decision for one assigned paper task. "
            "IRREVERSIBLE: the API rejects a second submission (409). Only call after the "
            "scientist approved this vote. Returns status submitted / already_completed / error."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task id from /slr list"},
                "decision": {"type": "string", "description": "INCLUDE, EXCLUDE or UNCERTAIN",
                             "maxLength": 100},
                "reasoning": {"type": "string", "description": "Reasoning text (max 16,000 chars)"},
                "data": {"type": "object", "description": "Optional structured data object"},
            },
            "required": ["task_id", "decision", "reasoning"],
            "additionalProperties": False,
        },
        "annotations": {"destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
    },
    {
        "name": "list_tasks",
        "title": "List SLR tasks",
        "description": ("List tasks assigned to this reviewer workspace's agent (all cursor pages), "
                        "including each task's stage and protocolSnapshot. The page also includes "
                        "current project context. Fallback for /slr list when "
                        "direct API reads from the sandbox fail."),
        "inputSchema": {
            "type": "object",
            "properties": {"status": {"type": "string", "enum": ["ASSIGNED", "COMPLETED", "any"],
                                      "default": "ASSIGNED"}},
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "get_markdown",
        "title": "Get paper full text",
        "description": ("Full-text Markdown for a FULL_TEXT task only, and only when the task has "
                        "markdownPath. Never call for TITLE_ABSTRACT tasks. Waits per Retry-After "
                        "while extraction is pending (up to max_wait seconds). Status: ready / "
                        "pending / unavailable."),
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"},
                           "max_wait": {"type": "integer", "default": 30, "minimum": 0, "maximum": 120}},
            "required": ["task_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "get_task",
        "title": "Get SLR task status",
        "description": ("Return the reviewer-scoped task, stage, protocolSnapshot, markdownPath, "
                        "paper metadata, and result. Use it to refresh a task or verify submission."),
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "connection_test",
        "title": "Test SLR API connection",
        "description": "Check that the connector can reach the API with its token; returns project name and task counts.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True},
    },
]


def log(msg):
    print(f"[slr-mcp] {msg}", file=sys.stderr, flush=True)


def tool_result(payload, is_error=False):
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
            "structuredContent": payload, "isError": is_error}


def call_tool(name, args):
    try:
        if name == "submit_result":
            body = build_body(args.get("decision"), args.get("reasoning"), args.get("data"))
            tid = str(args["task_id"])
            try:
                task = Client().submit(tid, body)
                return tool_result({"taskId": tid, "status": "submitted",
                                    "decision": body["decision"],
                                    "taskStatus": (task or {}).get("status")})
            except ApiError as e:
                if e.status == 409:
                    return tool_result({"taskId": tid, "status": "already_completed",
                                        "message": e.message})
                raise
        if name == "list_tasks":
            project, items = Client().list_tasks(status=args.get("status") or "ASSIGNED")
            return tool_result({"project": project, "count": len(items), "items": items})
        if name == "get_markdown":
            tid = str(args["task_id"])
            try:
                text = Client().markdown(tid, int(args.get("max_wait", 30)))
            except ApiError as e:
                if e.status == 202:
                    return tool_result({"taskId": tid, "status": "pending", "message": e.message})
                if e.status == 409:
                    return tool_result({"taskId": tid, "status": "unavailable", "message": e.message})
                raise
            return {"content": [{"type": "text", "text": text}],
                    "structuredContent": {"taskId": tid, "status": "ready", "chars": len(text)},
                    "isError": False}
        if name == "get_task":
            data = Client().get_task(str(args["task_id"]))
            return tool_result(data)
        if name == "connection_test":
            c = Client(retries=0)
            project, tasks = c.list_tasks(status="any")
            assigned = sum(1 for t in tasks if t.get("status") == "ASSIGNED")
            return tool_result({"connected": True, "api": c.base,
                                "project": (project or {}).get("name"),
                                "assigned": assigned, "completed": len(tasks) - assigned})
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
            "instructions": ("Connector for the /slr skill. submit_result only for votes the scientist "
                             "approved. list_tasks/get_markdown are the read fallback when the "
                             "sandbox cannot reach the API directly."),
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
    log("started")
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
