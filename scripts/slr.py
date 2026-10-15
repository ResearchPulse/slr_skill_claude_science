#!/usr/bin/env python3
"""Client for the AI Agent Task API (paper screening) — Claude Science edition.

Stdlib only; usable as a CLI or imported (the `slr` MCP connector imports it):

    import sys; sys.path.insert(0, "<skill>/scripts")
    from slr import Client
    c = Client()                      # reads config/token (never prints the token)
    project, tasks = c.list_tasks()

Configuration — one profile per reviewer workspace/project (the token is scoped to one agent,
reviewer, and project):
  config set-url https://<host>/api [--profile P]   save server URL
  config set-token [--profile P] [--stdin]          save token (hidden prompt, chmod 600)
  config use P                                      switch active profile
  config show                                       list profiles, token masked
  config test                                       call the API with the active profile
  config remove P                                   delete profile + token
  Global flag: --profile P (use P for one command without switching).

  Stored in ~/.config/slr/ (override dir with $REVIEW_CONFIG_DIR):
  config.json (URLs, current profile) and tokens/<profile> (0600).
  Env overrides, first match wins: $API_BASE_URL; $AGENT_TOKEN, $AGENT_TOKEN_FILE;
  $REVIEW_PROFILE.  Workdir: $REVIEW_WORKDIR -> ./paper-review

CLI commands:
  check                                  verify config + network + token, count tasks
  list      [--limit N] [--all] [--status ASSIGNED|COMPLETED|any]
  get       TASK_ID
  markdown  TASK_ID [--out FILE] [--max-wait S]   (default out: <workdir>/fulltext/<id>.md)
  draft     TASK_ID --decision D (--reasoning T | --reasoning-file F) [--data J | --data-file F]
  drafts                                 summary table of pending drafts
  submit-drafts [--ids ID ...]           POST pending drafts, log results
  submit    TASK_ID ...same args as draft...  [--dry-run]   (direct, no draft)

Exit codes: 0 ok, 1 error, 3 full text still extracting, 4 full text unavailable,
5 task already completed (409).
"""
import argparse
import datetime as _dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MAX_BODY_BYTES = 30 * 1024
MAX_REASONING = 16000
MAX_DECISION = 100
RETRYABLE = {500, 502, 503, 504}
USER_AGENT = "slr-agent-claude-science/1.0 (systematic-review screening CLI)"
CONF_DIR = Path.home() / ".config" / "slr"
DEFAULT_API_URL = "https://slr-agent.hyperdatalab.org/api"

EXIT_OK, EXIT_ERROR, EXIT_PENDING, EXIT_UNAVAILABLE, EXIT_CONFLICT = 0, 1, 3, 4, 5


class ApiError(Exception):
    def __init__(self, status, code, message, exit_code=EXIT_ERROR):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message, self.exit_code = status, code, message, exit_code

    def as_dict(self):
        return {"ok": False, "status": self.status, "code": self.code, "message": self.message}


# ------------------------------------------------------------------ config

def _conf_dir():
    return Path(os.environ.get("REVIEW_CONFIG_DIR") or CONF_DIR).expanduser()


def _read_conf():
    cfg = _conf_dir() / "config.json"
    conf = json.loads(cfg.read_text()) if cfg.exists() else {}
    # migrate the older flat layout {"api_base_url": ...}
    if "api_base_url" in conf and "profiles" not in conf:
        conf = {"current": "default", "profiles": {"default": {"api_base_url": conf["api_base_url"]}}}
    conf.setdefault("profiles", {})
    conf.setdefault("current", "default")
    return conf


def _write_conf(conf):
    d = _conf_dir()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    (d / "config.json").write_text(json.dumps(conf, indent=2), encoding="utf-8")


def active_profile(name=None):
    return name or os.environ.get("REVIEW_PROFILE") or _read_conf()["current"]


def _token_path(profile):
    p = _conf_dir() / "tokens" / profile
    legacy = _conf_dir() / "token"
    if profile == "default" and not p.exists() and legacy.exists():
        return legacy
    return p


def _load_base_url(profile=None):
    base = os.environ.get("API_BASE_URL", "").strip()
    if not base:
        prof = active_profile(profile)
        base = _read_conf()["profiles"].get(prof, {}).get("api_base_url", "")
    if not base:
        raise ApiError(None, "CONFIG",
                       "No API base URL. Run: slr.py config set-url https://<host>/api")
    return base.rstrip("/")


def _load_token(profile=None):
    tok = os.environ.get("AGENT_TOKEN", "").strip()
    if tok:
        return tok
    fp = os.environ.get("AGENT_TOKEN_FILE")
    p = Path(fp).expanduser() if fp else _token_path(active_profile(profile))
    if p.exists():
        tok = p.read_text().strip()
        if tok:
            return tok
    raise ApiError(None, "CONFIG",
                   "No agent token. Use /slr config token TOKEN, or run: slr.py config set-token")


def _mask(tok):
    return f"set (…{tok[-4:]}, {len(tok)} chars)" if len(tok) >= 12 else "set"


# Where drafts live when $REVIEW_WORKDIR is unset. The CLI uses ./paper-review; the MCP
# connector (whose cwd is not the chat's) swaps in ~/slr-paper-review/<profile>.
WORKDIR_DEFAULT = lambda: Path("paper-review")  # noqa: E731


def workdir():
    env = os.environ.get("REVIEW_WORKDIR")
    d = (Path(env).expanduser() if env else WORKDIR_DEFAULT()).resolve()
    for sub in ("fulltext", "drafts", "submitted"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


# ------------------------------------------------------------------ validation

CRITERION_STATUSES = ("met", "not_met", "unclear")
BASES = ("title_abstract", "full_text")


def validate_data(data):
    """The web app's "Criteria assessment" panel renders data.criteria; refuse votes without it.

    Same shape as the Claude Science skill:
      {"id": "IC1", "text": "<criterion text>", "status": met|not_met|unclear,
       "evidence": "<short quote / location>"}
    `name` is accepted for `text`, `evidence` may be a list, `reasoning` is optional.
    """
    if not isinstance(data, dict):
        raise ApiError(None, "VALIDATION", "data must be a JSON object with basis, confidence and criteria "
                       "(see 'Vote format' in the /slr skill).")
    if data.get("basis") not in BASES:
        raise ApiError(None, "VALIDATION", f"data.basis must be one of {', '.join(BASES)}.")
    conf = data.get("confidence")
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not 0 <= conf <= 1:
        raise ApiError(None, "VALIDATION", "data.confidence must be a number from 0 to 1.")
    crit = data.get("criteria")
    if not isinstance(crit, list) or not crit:
        raise ApiError(None, "VALIDATION", "data.criteria must list every IC/EC of the task's protocolSnapshot "
                       "— the web app shows it as 'Criteria assessment'.")
    for i, c in enumerate(crit):
        where = f"data.criteria[{i}]"
        if not isinstance(c, dict):
            raise ApiError(None, "VALIDATION", f"{where} must be an object.")
        filled = lambda v: isinstance(v, str) and v.strip()  # noqa: E731
        if not filled(c.get("id")):
            raise ApiError(None, "VALIDATION", f"{where}.id is required (IC1, EC1…).")
        if not (filled(c.get("text")) or filled(c.get("name"))):
            raise ApiError(None, "VALIDATION", f"{where}.text is required (the criterion text).")
        if c.get("status") not in CRITERION_STATUSES:
            raise ApiError(None, "VALIDATION", f"{where}.status must be one of {', '.join(CRITERION_STATUSES)}.")
        ev = c.get("evidence")
        if not (filled(ev) or (isinstance(ev, list) and all(filled(e) for e in ev))):
            raise ApiError(None, "VALIDATION", f"{where}.evidence must be a short quote/location "
                           "(text, or a list of them).")
    return data


def build_body(decision, reasoning, data=None):
    decision = (decision or "").strip()
    if not decision or len(decision) > MAX_DECISION:
        raise ApiError(None, "VALIDATION", "decision must be 1-100 characters.")
    reasoning = (reasoning or "").strip()
    if not reasoning:
        raise ApiError(None, "VALIDATION", "reasoning is required.")
    if len(reasoning) > MAX_REASONING:
        raise ApiError(None, "VALIDATION", f"reasoning is {len(reasoning)} chars; max {MAX_REASONING}.")
    body = {"decision": decision, "reasoning": reasoning, "data": validate_data(data)}
    size =len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
    if size > MAX_BODY_BYTES:
        raise ApiError(None, "VALIDATION", f"body is {size} bytes; max {MAX_BODY_BYTES}.")
    return body


def validate_task_result(task_response, body):
    """Reject stage-incompatible screening results before sending the POST."""
    task = task_response.get("task") or task_response
    stage = task.get("stage") or (task.get("reviewPolicy") or {}).get("stage") or "FULL_TEXT"
    if stage not in ("TITLE_ABSTRACT", "FULL_TEXT"):
        raise ApiError(None, "BAD_RESPONSE", f"Task returned unsupported screening stage: {stage}.")

    task_kind = task.get("taskKind")
    if task_kind != "screening":
        code = "EXTRACTION_TASK_REQUIRED" if task_kind == "extraction" else "TASK_KIND_REQUIRED"
        raise ApiError(409, code, "This task is not explicitly identified as a screening task.")

    decision = str(body.get("decision") or "").strip().upper()
    # Preserve the Claude Science screening alias used by older drafts.
    if stage == "TITLE_ABSTRACT" and decision == "INCLUDE":
        decision = "ADVANCE_TO_FULL_TEXT"
    allowed = ({"ADVANCE_TO_FULL_TEXT", "EXCLUDE", "UNCERTAIN"}
               if stage == "TITLE_ABSTRACT" else {"INCLUDE", "EXCLUDE", "UNCERTAIN"})
    if decision not in allowed:
        raise ApiError(None, "VALIDATION",
                       f"Decision {decision or '(empty)'} is not allowed for {stage}; "
                       f"allowed: {', '.join(sorted(allowed))}.")
    body["decision"] = decision

    if stage == "FULL_TEXT":
        paper = task_response.get("paper") or task.get("paper") or {}
        source_status = paper.get("fullTextStatus") or task.get("fullTextStatus")
        if source_status in {"PENDING", "PROCESSING", "PARTIAL"}:
            raise ApiError(409, "FULLTEXT_UNAVAILABLE",
                           f"Full-text source is {source_status}; wait for a COMPLETED source revision.")
        if source_status in {"FAILED", "UNAVAILABLE"} and decision != "UNCERTAIN":
            raise ApiError(None, "VALIDATION",
                           f"Only UNCERTAIN is allowed when full-text source is {source_status}.")
        if source_status != "COMPLETED" and source_status not in {"FAILED", "UNAVAILABLE"}:
            raise ApiError(None, "BAD_RESPONSE",
                           "FULL_TEXT task is missing a recognized paper.fullTextStatus.")
    return body


# ------------------------------------------------------------------ client

class Client:
    def __init__(self, base_url=None, token=None, retries=3, profile=None):
        self.base = (base_url or _load_base_url(profile)).rstrip("/")
        self._token = token or _load_token(profile)
        self.retries = retries
        host = (urllib.parse.urlparse(self.base).hostname or "").lower()
        if host in ("localhost", "127.0.0.1", "::1") or os.environ.get("SLR_NO_PROXY"):
            self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        else:
            self._opener = urllib.request.build_opener()

    def __repr__(self):  # keep the token out of kernel output
        return f"Client(base={self.base!r}, token=***)"

    def _request(self, method, path, body=None, accept="application/json"):
        if path.startswith("/api/") and self.base.endswith("/api"):
            path = path[len("/api"):]  # markdownPath is absolute (/api/...)
        url = self.base + path
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Bearer {self._token}", "Accept": accept,
                   "User-Agent": USER_AGENT}
        if data is not None:
            headers["Content-Type"] = "application/json"
        attempt = 0
        while True:
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with self._opener.open(req, timeout=60) as r:
                    return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
            except urllib.error.HTTPError as e:
                raw = e.read()
                if e.code in RETRYABLE and attempt < self.retries:
                    attempt += 1
                    time.sleep(2 ** attempt)
                    continue
                return e.code, {k.lower(): v for k, v in e.headers.items()}, raw
            except urllib.error.URLError as e:
                if attempt < self.retries:
                    attempt += 1
                    time.sleep(2 ** attempt)
                    continue
                raise ApiError(None, "NETWORK",
                               f"{e.reason}. Cannot reach {self.base}: check the backend is running and "
                               "the URL is right; from chat use the `slr` MCP connector; see `slr.py diag`.")

    @staticmethod
    def _raise(status, raw, exit_code=EXIT_ERROR):
        try:
            err = json.loads(raw.decode("utf-8")).get("error") or {}
        except Exception:
            err = {}
        raise ApiError(status, err.get("code", f"HTTP_{status}"),
                       err.get("message", raw.decode("utf-8", "replace")[:500]), exit_code)

    def _get_json(self, path):
        status, _, raw = self._request("GET", path)
        if status != 200:
            self._raise(status, raw)
        payload = json.loads(raw.decode("utf-8"))
        if not payload.get("success"):
            raise ApiError(status, "BAD_RESPONSE", "Unexpected response envelope.")
        return payload["data"]

    # --- endpoints
    def list_page(self, limit=50, cursor=None, stage=None):
        q = {"limit": max(1, min(100, limit))}
        if cursor:
            q["cursor"] = cursor
        if stage:
            if stage not in ("TITLE_ABSTRACT", "FULL_TEXT"):
                raise ApiError(None, "VALIDATION", "stage must be TITLE_ABSTRACT or FULL_TEXT.")
            q["stage"] = stage
        return self._get_json("/agent/v1/tasks?" + urllib.parse.urlencode(q))

    def list_tasks(self, status="ASSIGNED", limit=100, stage=None):
        items, project, cursor = [], None, None
        while True:
            page = self.list_page(limit, cursor, stage)
            project = project or page.get("project")
            items.extend(page.get("items", []))
            cursor = page.get("nextCursor")
            if not page.get("hasMore") or not cursor:
                break
        if status != "any":
            items = [t for t in items if t.get("status") == status]
        return project, items

    def list_consensus_tasks(self, status="any"):
        """List server-owned leader extraction tasks and provision them after R1/R2 submit."""
        data = self._get_json("/agent/v1/extraction/consensus-tasks")
        items = data.get("items", [])
        if status != "any":
            items = [task for task in items if task.get("status") == status]
        return data.get("project"), items, data.get("readiness", [])

    def get_task(self, task_id):
        return self._get_json(f"/agent/v1/tasks/{urllib.parse.quote(task_id)}")

    def markdown(self, task_id, max_wait=60):
        result = self.get_task(task_id)
        task = result.get("task") or result
        if task.get("stage") != "FULL_TEXT":
            raise ApiError(409, "FULLTEXT_STAGE_REQUIRED",
                           "Markdown is available only for FULL_TEXT tasks.", EXIT_UNAVAILABLE)
        path = task.get("markdownPath")
        if not path:
            raise ApiError(409, "FULLTEXT_UNAVAILABLE",
                           "This FULL_TEXT task has no markdownPath.", EXIT_UNAVAILABLE)
        if not isinstance(path, str) or not path.startswith(("/agent/v1/", "/api/agent/v1/")):
            raise ApiError(None, "BAD_RESPONSE", "Task returned an invalid markdownPath.")
        api_path = path[len("/api"):] if path.startswith("/api/") else path
        expected_path = f"/agent/v1/tasks/{urllib.parse.quote(task_id)}/markdown"
        if api_path != expected_path:
            raise ApiError(None, "BAD_RESPONSE", "Task returned a markdownPath for a different endpoint.")
        deadline = time.time() + max_wait
        while True:
            status, headers, raw = self._request("GET", path, accept="text/markdown")
            if status == 200:
                return raw.decode("utf-8", "replace")
            if status == 202:
                try:
                    wait = max(10, int(headers.get("retry-after", "10")))
                except ValueError:
                    wait = 10
                if time.time() + wait > deadline:
                    raise ApiError(202, "PENDING", "Full text still extracting; retry later.", EXIT_PENDING)
                time.sleep(wait)
                continue
            self._raise(status, raw, EXIT_UNAVAILABLE if status == 409 else EXIT_ERROR)

    def submit(self, task_id, body):
        path = f"/agent/v1/tasks/{urllib.parse.quote(task_id)}/result"
        status, _, raw = self._request("POST", path, body=body)
        if status == 200:
            return json.loads(raw.decode("utf-8")).get("data")
        try:
            code = (json.loads(raw.decode("utf-8")).get("error") or {}).get("code")
        except Exception:
            code = None
        completed = code in {"TASK_COMPLETED", "TASK_ALREADY_COMPLETED", "ALREADY_COMPLETED"}
        if status == 409 and not completed:
            try:
                current = self.get_task(task_id)
                current_task = current.get("task") or current
                completed = current_task.get("status") == "COMPLETED"
            except ApiError:
                completed = False
        self._raise(status, raw, EXIT_CONFLICT if status == 409 and completed else EXIT_ERROR)

    def extraction_request(self, method, task_id, action, body=None):
        path = f"/agent/v1/extraction/tasks/{urllib.parse.quote(task_id)}/{action}"
        status, _, raw = self._request(method, path, body=body)
        if status not in (200, 201, 202):
            self._raise(status, raw, EXIT_CONFLICT if status == 409 else EXIT_ERROR)
        if not raw:
            return {}
        payload = json.loads(raw.decode("utf-8"))
        if not payload.get("success"):
            raise ApiError(status, "BAD_RESPONSE", "Unexpected response envelope.")
        return payload.get("data")

    def get_extraction_consensus(self, task_id):
        """Read the leader-only consensus comparison for one extraction task."""
        return self.extraction_request("GET", task_id, "consensus")

    @staticmethod
    def _challenge_id(payload):
        if isinstance(payload, dict):
            for key in ("challengeId", "challenge_id", "challengeToken", "token"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    return value
            for key in ("challenge", "data"):
                found = Client._challenge_id(payload.get(key))
                if found:
                    return found
        return None

    @staticmethod
    def _challenge_value(payload, key):
        if isinstance(payload, dict):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
            for nested_key in ("challenge", "data"):
                found = Client._challenge_value(payload.get(nested_key), key)
                if found:
                    return found
        return None

    @staticmethod
    def _extraction_check_status(payload):
        if isinstance(payload, dict):
            status = payload.get("status")
            if status in {"PASS", "WARN", "BLOCK"}:
                return status
            for value in payload.values():
                status = Client._extraction_check_status(value)
                if status:
                    return status
        elif isinstance(payload, list):
            for value in payload:
                status = Client._extraction_check_status(value)
                if status:
                    return status
        return None

    def submit_extraction_proposal(self, task_id, consensus=False, proposal=None):
        if not consensus:
            check = self.extraction_request("POST", task_id, "check", {})
            if self._extraction_check_status(check) == "BLOCK":
                raise ApiError(None, "EXTRACTION_CHECK_BLOCKED",
                               "The current extraction draft has a BLOCK check and cannot be submitted.")
        challenge_action = "consensus-challenge" if consensus else "submit-challenge"
        submit_action = "consensus-proposal" if consensus else "submit"
        # The legacy leader route still uses the canonical consensus payload at
        # the top level. Do not wrap it in {"proposal": ...}; the backend
        # validates expectedVersion, values, and findings directly.
        challenge_body = (proposal or {}) if consensus else {}
        action_prefix = "mcp-consensus" if consensus else "mcp-extraction-direct-v2"
        return self._submit_extraction_challenge(task_id, challenge_action, submit_action,
                                                 challenge_body, action_prefix)

    def _submit_extraction_challenge(self, task_id, challenge_action, submit_action,
                                     challenge_body, action_prefix):
        challenge = self.extraction_request("POST", task_id, challenge_action, challenge_body)
        challenge_id = self._challenge_id(challenge)
        if not challenge_id:
            raise ApiError(None, "BAD_RESPONSE", "Challenge response did not include a challenge identifier.")
        payload_digest = self._challenge_value(challenge, "payloadDigest")
        if not payload_digest:
            raise ApiError(None, "BAD_RESPONSE", "Challenge response did not include a payload digest.")
        body = {
            "challengeId": challenge_id,
            "payloadDigest": payload_digest,
            "idempotencyKey": f"{action_prefix}-{task_id}-{payload_digest}",
        }
        return self.extraction_request("POST", task_id, submit_action, body)

    def save_extraction_consensus_draft(self, task_id, draft):
        return self._submit_extraction_challenge(
            task_id,
            "consensus-draft-challenge",
            "consensus-draft",
            draft,
            "mcp-consensus-draft-v1",
        )

    def submit_extraction_consensus(self, task_id, expected_version):
        return self._submit_extraction_challenge(
            task_id,
            "consensus-submit-challenge",
            "consensus-submit",
            {"expectedVersion": int(expected_version)},
            "mcp-consensus-official-v1",
        )


# ------------------------------------------------------------------ audit log

def log_event(event):
    event = {"at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"), **event}
    with open(workdir() / "audit.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


# ------------------------------------------------------------------ CLI helpers

def _out(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _read_args_body(args):
    reasoning = args.reasoning
    if args.reasoning_file:
        reasoning = Path(args.reasoning_file).read_text(encoding="utf-8")
    raw = args.data
    if args.data_file:
        raw = Path(args.data_file).read_text(encoding="utf-8")
    data = None
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ApiError(None, "VALIDATION", f"data is not valid JSON: {e}")
    return build_body(args.decision, reasoning, data)


def _proxy_view():
    def redact(u):
        p = urllib.parse.urlparse(u)
        return u.replace(p.password, "***") if p.password else u
    return {k: redact(v) for k, v in urllib.request.getproxies().items()
            if k in ("http", "https", "all", "no")}


def cmd_diag(args):
    import socket
    import ssl
    out = {"python": sys.version.split()[0], "platform": sys.platform,
           "proxies_seen_by_urllib": _proxy_view(),
           "ca_env": {k: os.environ.get(k) for k in
                      ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS")
                      if os.environ.get(k)},
           "default_ca_paths": ssl.get_default_verify_paths()._asdict()}
    try:
        base = _load_base_url()
        host = urllib.parse.urlparse(base).hostname
        out["api_base_url"] = base
        try:
            out["direct_dns"] = socket.gethostbyname(host)
        except OSError as e:
            out["direct_dns"] = f"failed ({e}) - DNS blocked or wrong host; from chat use the `slr` MCP connector"
    except ApiError as e:
        out["api_base_url"] = e.message
    try:
        r = _try_connect(None)
        out["api_call"] = r
    except ApiError as e:
        out["api_call"] = e.as_dict()
    if not (out["api_call"] or {}).get("connected"):
        out["hint"] = ("If this shell has no network (agent sandbox), use the `slr` MCP connector's "
                       "connection_test instead - it runs on the host.")
    _out(out)


def cmd_check(args):
    c = Client()
    project, tasks = c.list_tasks(status="any")
    assigned = sum(1 for t in tasks if t.get("status") == "ASSIGNED")
    _out({"ok": True, "api": c.base, "project": (project or {}).get("name"),
          "assigned": assigned, "completed": len(tasks) - assigned, "workdir": str(workdir())})


def cmd_list(args):
    c = Client()
    if args.all:
        project, items = c.list_tasks(status=args.status, limit=args.limit, stage=args.stage)
        nxt = None
    else:
        page = c.list_page(args.limit, stage=args.stage)
        project, items, nxt = page.get("project"), page.get("items", []), page.get("nextCursor")
        if args.status != "any":
            items = [t for t in items if t.get("status") == args.status]
    _out({"ok": True, "project": project, "count": len(items), "nextCursor": nxt, "items": items})


def cmd_get(args):
    _out({"ok": True, **Client().get_task(args.task_id)})


def cmd_markdown(args):
    text = Client().markdown(args.task_id, args.max_wait)
    out = Path(args.out) if args.out else workdir() / "fulltext" / f"{args.task_id}.md"
    out.write_text(text, encoding="utf-8")
    _out({"ok": True, "file": str(out), "chars": len(text)})


# Library form (return dicts) — shared by the CLI below and the MCP connector's draft tools.

def save_draft(task_id, body, title=""):
    body = build_body(body.get("decision"), body.get("reasoning"), body.get("data"))
    path = workdir() / "drafts" / f"{task_id}.json"
    replaced = path.exists()
    path.write_text(json.dumps({"taskId": task_id, "title": title or "", "body": body},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    log_event({"event": "edit" if replaced else "draft", "taskId": task_id, "decision": body["decision"]})
    return {"ok": True, "taskId": task_id, "draft": str(path), "decision": body["decision"],
            "replaced": replaced}


def _load_drafts(ids=None):
    drafts = []
    for p in sorted((workdir() / "drafts").glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        if ids and d["taskId"] not in ids:
            continue
        d["_path"] = p
        drafts.append(d)
    return drafts


def get_draft(task_id):
    for sub in ("drafts", "submitted"):
        p = workdir() / sub / f"{task_id}.json"
        if p.exists():
            return {"ok": True, "state": "pending" if sub == "drafts" else "submitted",
                    **json.loads(p.read_text(encoding="utf-8"))}
    raise ApiError(None, "NOT_FOUND", f"No draft for task {task_id}.")


def drafts_table():
    rows = []
    for d in _load_drafts():
        b = d["body"]
        rows.append({"taskId": d["taskId"], "title": (d.get("title") or "")[:80],
                     "decision": b["decision"],
                     "confidence": (b.get("data") or {}).get("confidence"),
                     "keyReason": next((l.split(":", 1)[1].strip() for l in b["reasoning"].splitlines()
                                        if l.lower().startswith("key reason")), b["reasoning"][:120])})
    counts = {}
    for r in rows:
        counts[r["decision"]] = counts.get(r["decision"], 0) + 1
    done = sorted(p.stem for p in (workdir() / "submitted").glob("*.json"))
    return {"ok": True, "workdir": str(workdir()), "pending": len(rows), "byDecision": counts,
            "drafts": rows, "submittedIds": done}


def mark_submitted(task_id, result, via="mcp", error=None):
    """Record a vote that was sent by another channel."""
    src = workdir() / "drafts" / f"{task_id}.json"
    moved = result in ("submitted", "already_completed") and src.exists()
    if moved:
        src.replace(workdir() / "submitted" / src.name)
    log_event({"event": "submit", "taskId": task_id, "result": result, "via": via,
               **({"error": error} if error else {})})
    return {"ok": True, "taskId": task_id, "result": result, "draftMoved": moved}


def submit_drafts(ids=None):
    """Refresh and validate each task before POST; move accepted drafts to submitted/. Stop on 401."""
    c = Client()
    results = []
    for d in _load_drafts(set(ids) if ids else None):
        tid = d["taskId"]
        try:
            body = build_body(d["body"].get("decision"), d["body"].get("reasoning"), d["body"].get("data"))
            validate_task_result(c.get_task(tid), body)
            c.submit(tid, body)
            res = {"taskId": tid, "result": "submitted", "decision": body["decision"]}
        except ApiError as e:
            if e.exit_code == EXIT_CONFLICT:
                res = {"taskId": tid, "result": "already_completed"}
            elif e.status == 401:
                log_event({"event": "submit_abort", "taskId": tid, "error": e.as_dict()})
                results.append({"taskId": tid, "result": "error", "error": e.as_dict()})
                break  # token is dead; stop the batch
            else:
                res = {"taskId": tid, "result": "error", "error": e.as_dict()}
        if res["result"] != "error":
            d["_path"].replace(workdir() / "submitted" / d["_path"].name)
        log_event({"event": "submit", **res})
        results.append(res)
    missing = sorted(set(ids or []) - {r["taskId"] for r in results})
    return {"ok": all(r["result"] != "error" for r in results) and not missing,
            "results": results, **({"noDraft": missing} if missing else {})}


def cmd_draft(args):
    _out(save_draft(args.task_id, _read_args_body(args), args.title))


def cmd_drafts(args):
    _out(drafts_table())


def cmd_mark_submitted(args):
    _out(mark_submitted(args.task_id, args.result, args.via, args.error))


def cmd_submit_drafts(args):
    res = submit_drafts(args.ids)
    _out(res)
    if not res["ok"]:
        sys.exit(EXIT_ERROR)


def cmd_submit(args):
    body = _read_args_body(args)
    if args.dry_run:
        _out({"ok": True, "dryRun": True, "body": body})
        return
    client = Client()
    validate_task_result(client.get_task(args.task_id), body)
    task = client.submit(args.task_id, body)
    log_event({"event": "submit", "taskId": args.task_id, "result": "submitted",
               "decision": body["decision"]})
    _out({"ok": True, "task": task})


# ------------------------------------------------------------------ config commands

def _normalize_url(url):
    url = url.strip().rstrip("/")
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ApiError(None, "VALIDATION", "URL must look like https://<backend-host>/api")
    if not u.path.endswith("/api"):
        url += "/api"  # contract paths are /api/agent/v1/...
    return url


def _try_connect(profile):
    try:
        c = Client(profile=profile, retries=0)
        project, tasks = c.list_tasks(status="any")
        assigned = sum(1 for t in tasks if t.get("status") == "ASSIGNED")
        return {"connected": True, "project": (project or {}).get("name"),
                "assigned": assigned, "completed": len(tasks) - assigned}
    except ApiError as e:
        return {"connected": False, "error": e.as_dict()}


# Library form (return dicts) — shared by the CLI below and the MCP connector's config_* tools.

def set_url(url, profile=None):
    conf = _read_conf()
    prof = profile or conf["current"]
    url = _normalize_url(url)
    conf["profiles"].setdefault(prof, {})["api_base_url"] = url
    if profile and not conf["profiles"].get(conf["current"]):
        conf["current"] = prof
    _write_conf(conf)
    return {"ok": True, "profile": prof, "api_base_url": url}


def set_token(tok, profile=None):
    conf = _read_conf()
    prof = profile or conf["current"]
    tok = (tok or "").strip()
    if tok.lower().startswith("bearer "):
        tok = tok[7:].strip()
    if not tok or any(ch.isspace() for ch in tok):
        raise ApiError(None, "VALIDATION", "Token is empty or contains whitespace.")
    p = _conf_dir() / "tokens" / prof
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(_conf_dir(), 0o700)
    os.chmod(p.parent, 0o700)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok)
    os.chmod(p, 0o600)
    conf["profiles"].setdefault(prof, {})
    _write_conf(conf)
    res = {"ok": True, "profile": prof, "token": _mask(tok)}
    if conf["profiles"][prof].get("api_base_url"):
        res.update(_try_connect(prof))
    return res


def use_profile(profile):
    conf = _read_conf()
    if profile not in conf["profiles"]:
        raise ApiError(None, "CONFIG", f"Unknown profile '{profile}'. "
                       f"Known: {', '.join(conf['profiles']) or 'none'}")
    conf["current"] = profile
    _write_conf(conf)
    return {"ok": True, "current": profile}


def show_config():
    conf = _read_conf()
    cur = active_profile()
    rows = []
    for name in sorted(set(conf["profiles"]) | {cur}):
        tp = _token_path(name)
        tok = tp.read_text().strip() if tp.exists() else ""
        rows.append({"profile": name, "active": name == cur,
                     "api_base_url": conf["profiles"].get(name, {}).get("api_base_url"),
                     "token": _mask(tok) if tok else "missing"})
    overrides = [k for k in ("API_BASE_URL", "AGENT_TOKEN", "AGENT_TOKEN_FILE", "REVIEW_PROFILE")
                 if os.environ.get(k)]
    return {"ok": True, "configDir": str(_conf_dir()), "profiles": rows, "envOverrides": overrides}


def remove_profile(profile):
    conf = _read_conf()
    conf["profiles"].pop(profile, None)
    for p in (_conf_dir() / "tokens" / profile, _token_path(profile)):
        if p.exists():
            p.unlink()
    if conf["current"] == profile:
        conf["current"] = next(iter(conf["profiles"]), "default")
    _write_conf(conf)
    return {"ok": True, "removed": profile, "current": conf["current"]}


def cmd_config_set_url(args):
    _out(set_url(args.url, args.profile))


def cmd_config_set_token(args):
    prof = args.profile or _read_conf()["current"]
    if args.stdin or not sys.stdin.isatty():
        tok = sys.stdin.read()
    else:
        import getpass
        tok = getpass.getpass(f"Agent token for profile '{prof}' (input hidden): ")
    _out(set_token(tok, prof))


def cmd_config_use(args):
    _out(use_profile(args.profile))


def cmd_config_show(args):
    _out(show_config())


def cmd_config_test(args):
    prof = active_profile(args.profile)
    res = {"profile": prof, **_try_connect(prof)}
    _out({"ok": res["connected"], **res})
    if not res["connected"]:
        sys.exit(EXIT_ERROR)


def cmd_config_remove(args):
    _out(remove_profile(args.profile))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile", help="Use this profile for this command (overrides current)")
    sub = p.add_subparsers(dest="cmd", required=True)

    cfg = sub.add_parser("config", help="Configure API server and token")
    csub = cfg.add_subparsers(dest="config_cmd", required=True)
    s = csub.add_parser("set-url", help="Set API base URL, e.g. https://host/api")
    s.add_argument("url"); s.add_argument("--profile", dest="cprofile")
    s.set_defaults(func=cmd_config_set_url)
    s = csub.add_parser("set-token", help="Save agent token (hidden prompt or --stdin)")
    s.add_argument("--stdin", action="store_true"); s.add_argument("--profile", dest="cprofile")
    s.set_defaults(func=cmd_config_set_token)
    s = csub.add_parser("use", help="Switch active profile"); s.add_argument("profile_name")
    s.set_defaults(func=cmd_config_use)
    csub.add_parser("show", help="Show profiles (token masked)").set_defaults(func=cmd_config_show)
    s = csub.add_parser("test", help="Test connection with current profile")
    s.set_defaults(func=cmd_config_test)
    s = csub.add_parser("remove", help="Delete a profile and its token"); s.add_argument("profile_name")
    s.set_defaults(func=cmd_config_remove)

    sub.add_parser("check").set_defaults(func=cmd_check)
    sub.add_parser("diag", help="Network/proxy/CA diagnostics (no secrets)").set_defaults(func=cmd_diag)

    s = sub.add_parser("list")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--all", action="store_true")
    s.add_argument("--status", default="ASSIGNED")
    s.add_argument("--stage", choices=["TITLE_ABSTRACT", "FULL_TEXT"])
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("get"); s.add_argument("task_id"); s.set_defaults(func=cmd_get)

    s = sub.add_parser("markdown")
    s.add_argument("task_id"); s.add_argument("--out"); s.add_argument("--max-wait", type=int, default=60)
    s.set_defaults(func=cmd_markdown)

    for name, fn in (("draft", cmd_draft), ("submit", cmd_submit)):
        s = sub.add_parser(name)
        s.add_argument("task_id")
        s.add_argument("--decision", required=True)
        s.add_argument("--reasoning"); s.add_argument("--reasoning-file")
        s.add_argument("--data"); s.add_argument("--data-file")
        if name == "draft":
            s.add_argument("--title", default="")
        else:
            s.add_argument("--dry-run", action="store_true")
        s.set_defaults(func=fn)

    sub.add_parser("drafts").set_defaults(func=cmd_drafts)

    s = sub.add_parser("mark-submitted", help="Record a vote sent via MCP; moves the draft")
    s.add_argument("task_id")
    s.add_argument("--result", required=True, choices=["submitted", "already_completed", "error"])
    s.add_argument("--via", default="mcp")
    s.add_argument("--error")
    s.set_defaults(func=cmd_mark_submitted)

    s = sub.add_parser("submit-drafts"); s.add_argument("--ids", nargs="*")
    s.set_defaults(func=cmd_submit_drafts)

    args = p.parse_args()
    if getattr(args, "profile_name", None):
        args.profile = args.profile_name
    elif getattr(args, "cprofile", None):
        args.profile = args.cprofile
    if args.profile and args.cmd != "config":
        os.environ["REVIEW_PROFILE"] = args.profile  # every Client() in this run uses it
    try:
        args.func(args)
    except ApiError as e:
        print(json.dumps(e.as_dict(), ensure_ascii=False))
        sys.exit(e.exit_code)


if __name__ == "__main__":
    main()
