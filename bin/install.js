#!/usr/bin/env node
/*
 * slr-skill — installer for the /slr skill and its MCP connector.
 *
 *   npx github:<user>/<repo>                 interactive install
 *   npx github:<user>/<repo> install --url https://host/api --token XXX -y
 *   npx github:<user>/<repo> mcp             print the MCP config only
 *   npx github:<user>/<repo> doctor          check python / skill / connection
 *   npx github:<user>/<repo> uninstall       remove skill + MCP entries
 *
 * No npm dependencies. Needs Python 3.8+ on the machine (the skill is stdlib-only).
 */
'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const readline = require('readline');
const { spawnSync } = require('child_process');

const PKG_ROOT = path.resolve(__dirname, '..');
const PKG = JSON.parse(fs.readFileSync(path.join(PKG_ROOT, 'package.json'), 'utf8'));
const SKILL_NAME = 'slr';
const MCP_NAME = 'slr';
const SKILL_ENTRIES = ['SKILL.md', 'scripts', 'references'];
const HOME = os.homedir();
const IS_WIN = process.platform === 'win32';

const SKILL_TARGETS = {
  code: {
    label: 'Claude Code',
    root: process.env.CLAUDE_CONFIG_DIR || path.join(HOME, '.claude'),
  },
  science: {
    label: 'Claude Science',
    root: path.join(HOME, '.claude-science'),
  },
};
for (const t of Object.values(SKILL_TARGETS)) t.dir = path.join(t.root, 'skills', SKILL_NAME);

// ------------------------------------------------------------------ output

const useColor = process.stdout.isTTY && !process.env.NO_COLOR;
const paint = (code) => (s) => (useColor ? `\x1b[${code}m${s}\x1b[0m` : String(s));
const c = { bold: paint(1), dim: paint(2), red: paint(31), green: paint(32), yellow: paint(33), cyan: paint(36) };
const ok = (m) => console.log(`${c.green('✔')} ${m}`);
const warn = (m) => console.log(`${c.yellow('!')} ${m}`);
const fail = (m) => console.log(`${c.red('✖')} ${m}`);
const info = (m) => console.log(`${c.cyan('›')} ${m}`);
const step = (m) => console.log(`\n${c.bold(m)}`);

// ------------------------------------------------------------------ args

function parseArgs(argv) {
  const opts = { cmd: 'install', yes: false };
  const flags = {
    '--url': 'url', '--token': 'token', '--profile': 'profile', '--target': 'target',
    '--mcp': 'mcp', '--python': 'python',
  };
  const rest = [];
  for (let i = 0; i < argv.length; i++) {
    let a = argv[i];
    let val;
    if (a.startsWith('--') && a.includes('=')) [a, val] = [a.slice(0, a.indexOf('=')), a.slice(a.indexOf('=') + 1)];
    if (flags[a]) {
      opts[flags[a]] = val !== undefined ? val : argv[++i];
      if (opts[flags[a]] === undefined) die(`Missing value for ${a}`);
    } else if (a === '-y' || a === '--yes') opts.yes = true;
    else if (a === '--token-in-env') opts.tokenInEnv = true;
    else if (a === '--purge') opts.purge = true;
    else if (a === '--no-mcp') opts.mcp = 'none';
    else if (a === '-h' || a === '--help') opts.cmd = 'help';
    else if (a === '-v' || a === '--version') opts.cmd = 'version';
    else if (a.startsWith('-')) die(`Unknown option ${a} (see --help)`);
    else rest.push(a);
  }
  if (rest[0]) opts.cmd = rest[0];
  if (!opts.token && process.env.SLR_TOKEN) opts.token = process.env.SLR_TOKEN;
  if (!opts.url && process.env.SLR_API_URL) opts.url = process.env.SLR_API_URL;
  return opts;
}

function die(msg) {
  fail(msg);
  process.exit(1);
}

const HELP = `
${c.bold('slr-skill')} v${PKG.version} — install the /slr skill and its MCP connector

${c.bold('Usage')}
  npx github:${repoSlug()} [command] [options]

${c.bold('Commands')}
  install     (default) copy the skill, configure URL/token, register the MCP server
  mcp         print the MCP server JSON (for Claude Science / any MCP client)
  doctor      check Python, installed skill, MCP entries and API connection
  uninstall   remove the skill folders and MCP entries (--purge also deletes ~/.config/slr)

${c.bold('Options')}
  --target <t>     where to install the skill: code, science, all, or a folder path
                   (comma-separated allowed). Default: code + science if ~/.claude-science exists
  --url <url>      API base URL, e.g. http://localhost:5000/api   (env SLR_API_URL)
  --token <tok>    agent token for the project                    (env SLR_TOKEN)
  --profile <p>    store URL/token under this profile (one profile per project)
  --mcp <list>     MCP clients to register: code, desktop, none (comma-separated).
                   Default: every client found on this machine
  --no-mcp         same as --mcp none
  --token-in-env   also put API_BASE_URL / AGENT_TOKEN into the MCP config env
                   (default: the MCP server reads the profile saved in ~/.config/slr)
  --python <cmd>   Python 3 interpreter to use (default: auto-detect py / python3 / python)
  -y, --yes        non-interactive: never prompt, skip anything not given by flags
  -h, --help       show this help
`;

// ------------------------------------------------------------------ prompts

const interactive = (opts) => !opts.yes && process.stdin.isTTY && process.stdout.isTTY;

function ask(question, { hidden = false, def = '' } = {}) {
  return new Promise((resolve) => {
    const rl = readline.createInterface({ input: process.stdin, output: process.stdout, terminal: true });
    const suffix = def ? c.dim(` [${def}]`) : '';
    if (hidden) {
      rl._writeToOutput = function (s) {
        if (s.includes(question)) rl.output.write(s);
        else if (s === '\r\n' || s === '\n' || s === '\r') rl.output.write('\n');
        else rl.output.write('*'.repeat(s.length));
      };
    }
    rl.question(`${c.cyan('?')} ${question}${suffix} `, (ans) => {
      rl.close();
      if (hidden) process.stdout.write('\n');
      resolve(ans.trim() || def);
    });
  });
}

async function confirm(question, opts, def = true) {
  if (!interactive(opts)) return def;
  const ans = (await ask(`${question} ${c.dim(def ? '(Y/n)' : '(y/N)')}`)).toLowerCase();
  if (!ans) return def;
  return ans.startsWith('y') || ans.startsWith('c'); // "c" = "có"
}

// ------------------------------------------------------------------ python

function findPython(opts) {
  const probe = 'import sys;print(sys.version_info[0],sys.version_info[1]);print(sys.executable)';
  const candidates = opts.python
    ? [opts.python.split(' ')]
    : IS_WIN ? [['py', '-3'], ['python'], ['python3']] : [['python3'], ['python']];
  for (const [cmd, ...pre] of candidates) {
    const r = spawnSync(cmd, [...pre, '-c', probe], { encoding: 'utf8', windowsHide: true });
    if (r.status !== 0 || !r.stdout) continue;
    const [verLine, exe] = r.stdout.trim().split(/\r?\n/);
    const [major, minor] = verLine.split(' ').map(Number);
    if (major === 3 && minor >= 8 && exe) return { exe: exe.trim(), version: `${major}.${minor}` };
  }
  return null;
}

function runPy(py, script, args, input) {
  const r = spawnSync(py.exe, [script, ...args], {
    encoding: 'utf8', input, windowsHide: true,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
  });
  let json = null;
  try { json = JSON.parse(r.stdout); } catch (_) { /* not JSON */ }
  return { status: r.status, stdout: r.stdout || '', stderr: r.stderr || '', json };
}

// ------------------------------------------------------------------ skill files

function resolveTargets(opts) {
  if (!opts.target) {
    const t = ['code'];
    if (fs.existsSync(SKILL_TARGETS.science.root)) t.push('science');
    return t.map((k) => ({ key: k, ...SKILL_TARGETS[k] }));
  }
  const out = [];
  for (const raw of opts.target.split(',').map((s) => s.trim()).filter(Boolean)) {
    if (raw === 'all') out.push(...Object.keys(SKILL_TARGETS).map((k) => ({ key: k, ...SKILL_TARGETS[k] })));
    else if (SKILL_TARGETS[raw]) out.push({ key: raw, ...SKILL_TARGETS[raw] });
    else {
      const dir = path.resolve(raw.replace(/^~(?=$|[\\/])/, HOME));
      out.push({ key: 'custom', label: dir, dir: path.basename(dir) === SKILL_NAME ? dir : path.join(dir, SKILL_NAME) });
    }
  }
  const seen = new Set();
  return out.filter((t) => !seen.has(t.dir) && seen.add(t.dir));
}

function copySkill(dir) {
  fs.mkdirSync(dir, { recursive: true });
  for (const entry of SKILL_ENTRIES) {
    const dst = path.join(dir, entry);
    fs.rmSync(dst, { recursive: true, force: true });
    fs.cpSync(path.join(PKG_ROOT, entry), dst, {
      recursive: true,
      filter: (src) => !/__pycache__|\.pyc$/.test(src),
    });
  }
}

function findInstalledSkills() {
  return Object.entries(SKILL_TARGETS)
    .filter(([, t]) => fs.existsSync(path.join(t.dir, 'SKILL.md')))
    .map(([key, t]) => ({ key, ...t }));
}

// ------------------------------------------------------------------ MCP config

function mcpEntry(py, scriptsDir, opts, url, token) {
  const entry = { command: py.exe, args: [path.join(scriptsDir, 'slr_mcp.py')] };
  const env = {};
  if (opts.profile) env.REVIEW_PROFILE = opts.profile;
  if (opts.tokenInEnv) {
    if (url) env.API_BASE_URL = url;
    if (token) env.AGENT_TOKEN = token;
  }
  if (Object.keys(env).length) entry.env = env;
  return entry;
}

function desktopConfigPath() {
  if (IS_WIN) return path.join(process.env.APPDATA || path.join(HOME, 'AppData', 'Roaming'), 'Claude', 'claude_desktop_config.json');
  if (process.platform === 'darwin') return path.join(HOME, 'Library', 'Application Support', 'Claude', 'claude_desktop_config.json');
  return path.join(process.env.XDG_CONFIG_HOME || path.join(HOME, '.config'), 'Claude', 'claude_desktop_config.json');
}

function readJson(file) {
  if (!fs.existsSync(file)) return {};
  const txt = fs.readFileSync(file, 'utf8').replace(/^﻿/, '');
  return txt.trim() ? JSON.parse(txt) : {};
}

function writeJsonWithBackup(file, data) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  if (fs.existsSync(file)) fs.copyFileSync(file, `${file}.bak`);
  fs.writeFileSync(file, JSON.stringify(data, null, 2) + '\n', 'utf8');
}

function runClaude(args) {
  let r = spawnSync('claude', args, { encoding: 'utf8', windowsHide: true });
  if (r.error && r.error.code === 'ENOENT' && IS_WIN) {
    // npm-installed Claude Code on Windows is a .cmd shim, which needs a shell
    const quoted = args.map((a) => (/[\s"&|<>^]/.test(a) ? `"${a.replace(/"/g, '""')}"` : a));
    r = spawnSync('claude.cmd', quoted, { encoding: 'utf8', windowsHide: true, shell: true });
  }
  return r;
}

const hasClaudeCli = () => {
  const r = runClaude(['--version']);
  return !r.error && r.status === 0;
};

function registerClaudeCode(entry) {
  if (hasClaudeCli()) {
    runClaude(['mcp', 'remove', MCP_NAME, '-s', 'user']); // ignore "not found"
    const args = ['mcp', 'add', '-s', 'user'];
    for (const [k, v] of Object.entries(entry.env || {})) args.push('-e', `${k}=${v}`);
    args.push(MCP_NAME, '--', entry.command, ...entry.args);
    const r = runClaude(args);
    if (r.status === 0) return 'claude mcp add (user scope)';
    warn(`claude mcp add failed: ${(r.stderr || r.stdout || '').trim()} — editing ~/.claude.json instead`);
  }
  const file = path.join(HOME, '.claude.json');
  const data = readJson(file);
  data.mcpServers = { ...(data.mcpServers || {}), [MCP_NAME]: { type: 'stdio', ...entry } };
  writeJsonWithBackup(file, data);
  return file;
}

function unregisterClaudeCode() {
  if (hasClaudeCli()) {
    const r = runClaude(['mcp', 'remove', MCP_NAME, '-s', 'user']);
    return r.status === 0;
  }
  const file = path.join(HOME, '.claude.json');
  const data = readJson(file);
  if (!data.mcpServers || !data.mcpServers[MCP_NAME]) return false;
  delete data.mcpServers[MCP_NAME];
  writeJsonWithBackup(file, data);
  return true;
}

function registerDesktop(entry) {
  const file = desktopConfigPath();
  const data = readJson(file);
  data.mcpServers = { ...(data.mcpServers || {}), [MCP_NAME]: entry };
  writeJsonWithBackup(file, data);
  return file;
}

function unregisterDesktop() {
  const file = desktopConfigPath();
  const data = readJson(file);
  if (!data.mcpServers || !data.mcpServers[MCP_NAME]) return false;
  delete data.mcpServers[MCP_NAME];
  writeJsonWithBackup(file, data);
  return true;
}

function resolveMcpClients(opts) {
  if (opts.mcp) {
    const list = opts.mcp.split(',').map((s) => s.trim()).filter(Boolean);
    if (list.includes('none')) return [];
    for (const k of list) if (!['code', 'desktop'].includes(k)) die(`Unknown --mcp client "${k}" (use code, desktop, none)`);
    return list;
  }
  const list = [];
  if (hasClaudeCli() || fs.existsSync(SKILL_TARGETS.code.root)) list.push('code');
  if (fs.existsSync(path.dirname(desktopConfigPath()))) list.push('desktop');
  return list;
}

function printMcpSnippet(entry) {
  const snippet = { mcpServers: { [MCP_NAME]: entry } };
  console.log(c.dim('─'.repeat(60)));
  console.log(JSON.stringify(snippet, null, 2));
  console.log(c.dim('─'.repeat(60)));
}

// ------------------------------------------------------------------ profile helpers

function readProfile(py, slrPy, profile) {
  const r = runPy(py, slrPy, ['config', 'show']);
  if (!r.json || !r.json.profiles) return {};
  const want = profile || (r.json.profiles.find((p) => p.active) || {}).profile;
  return r.json.profiles.find((p) => p.profile === want) || {};
}

const profileArgs = (opts) => (opts.profile ? ['--profile', opts.profile] : []);

// ------------------------------------------------------------------ commands

async function cmdInstall(opts) {
  console.log(c.bold(`\n/slr skill installer v${PKG.version}`));

  step('1. Python');
  const py = findPython(opts);
  if (!py) {
    die('Python 3.8+ not found. Install it from https://www.python.org/downloads/ '
      + '(Windows: tick "Add python.exe to PATH"), or pass --python <path>.');
  }
  ok(`Python ${py.version} — ${py.exe}`);

  step('2. Skill files');
  const targets = resolveTargets(opts);
  for (const t of targets) {
    copySkill(t.dir);
    ok(`${t.label}: ${t.dir}`);
  }
  const primary = targets[0];
  const scriptsDir = path.join(primary.dir, 'scripts');
  const slrPy = path.join(scriptsDir, 'slr.py');

  step('3. API server & agent token');
  const current = readProfile(py, slrPy, opts.profile);
  let url = opts.url;
  if (!url && interactive(opts)) {
    url = await ask('API base URL (as seen from this machine):',
      { def: current.api_base_url || 'http://localhost:5000/api' });
  }
  url = url || current.api_base_url;
  if (url && url !== current.api_base_url) {
    const r = runPy(py, slrPy, [...profileArgs(opts), 'config', 'set-url', url]);
    if (r.json && r.json.ok) { url = r.json.api_base_url; ok(`URL saved: ${url} (profile ${r.json.profile})`); }
    else { fail(`Could not save URL: ${(r.json && r.json.message) || r.stderr.trim()}`); url = current.api_base_url; }
  } else if (url) ok(`URL: ${url}`);
  else warn('No URL set — run /slr setup later, or re-run with --url');

  let token = opts.token;
  const hasToken = current.token && current.token !== 'missing';
  if (!token && interactive(opts)) {
    const q = hasToken ? `Agent token (Enter to keep current ${current.token}):` : 'Agent token (input hidden, Enter to skip):';
    token = await ask(q, { hidden: true });
  }
  if (token) {
    const r = runPy(py, slrPy, [...profileArgs(opts), 'config', 'set-token', '--stdin'], token);
    if (r.json && r.json.ok) {
      ok(`Token saved: ${r.json.token} (profile ${r.json.profile})`);
      if (r.json.connected === true) ok(`Connected — project "${r.json.project}", ${r.json.assigned} assigned / ${r.json.completed} completed`);
      else if (r.json.connected === false) warn(`Saved, but connection test failed: ${JSON.stringify(r.json.error)}`);
    } else fail(`Could not save token: ${(r.json && r.json.message) || r.stderr.trim()}`);
  } else if (hasToken) {
    ok(`Token: ${current.token}`);
    const t = runPy(py, slrPy, [...profileArgs(opts), 'config', 'test']);
    if (t.json && t.json.connected) ok(`Connected — project "${t.json.project}", ${t.json.assigned} assigned`);
    else if (url) warn(`Connection test failed: ${JSON.stringify((t.json && t.json.error) || t.stderr.trim())}`);
  } else warn('No token set — use /slr config token later, or re-run with --token');

  step('4. MCP connector');
  const entry = mcpEntry(py, scriptsDir, opts, url, token);
  const clients = resolveMcpClients(opts);
  if (!clients.length) info('No MCP client selected/found — skipping automatic registration.');
  for (const client of clients) {
    try {
      if (client === 'code') ok(`Claude Code: registered via ${registerClaudeCode(entry)}`);
      if (client === 'desktop') ok(`Claude Desktop: ${registerDesktop(entry)} (restart the app)`);
    } catch (e) {
      fail(`${client}: ${e.message}`);
    }
  }
  const snippetFile = path.join(primary.dir, 'mcp-config.json');
  fs.writeFileSync(snippetFile, JSON.stringify({ mcpServers: { [MCP_NAME]: entry } }, null, 2) + '\n', 'utf8');
  if (targets.some((t) => t.key === 'science') || !clients.length) {
    info('Claude Science / other clients: Settings → Connectors (MCP servers) → add this JSON');
    info(`(also saved to ${snippetFile})`);
    printMcpSnippet(entry);
  }

  step('Done');
  console.log(`  Open Claude and type ${c.bold('/slr setup')} or ${c.bold('/slr status')}.`);
  console.log(`  Check everything any time:  ${c.bold(`npx github:${repoSlug()} doctor`)}\n`);
}

function cmdMcp(opts) {
  const py = findPython(opts) || die('Python 3.8+ not found (use --python).');
  const installed = findInstalledSkills();
  const dir = installed.length ? installed[0].dir : resolveTargets(opts)[0].dir;
  if (!installed.length) warn(`Skill not installed yet — run install first. Path below assumes ${dir}`);
  const cur = readProfile(py, path.join(dir, 'scripts', 'slr.py'), opts.profile);
  printMcpSnippet(mcpEntry(py, path.join(dir, 'scripts'), opts, opts.url || cur.api_base_url, opts.token));
}

function cmdDoctor(opts) {
  let bad = 0;
  step('Python');
  const py = findPython(opts);
  if (py) ok(`Python ${py.version} — ${py.exe}`); else { fail('Python 3.8+ not found'); bad++; }

  step('Skill');
  const installed = findInstalledSkills();
  if (!installed.length) { fail('Skill not installed (run install)'); bad++; }
  for (const t of installed) ok(`${t.label}: ${t.dir}`);

  step('MCP entries');
  const desk = readJson(desktopConfigPath());
  if (desk.mcpServers && desk.mcpServers[MCP_NAME]) ok(`Claude Desktop: ${desktopConfigPath()}`);
  if (hasClaudeCli()) {
    const r = runClaude(['mcp', 'get', MCP_NAME]);
    if (r.status === 0) ok('Claude Code: registered'); else warn('Claude Code: not registered');
  }
  if (installed.length) {
    const snip = path.join(installed[0].dir, 'mcp-config.json');
    if (fs.existsSync(snip)) info(`Claude Science snippet: ${snip}`);
  }

  if (py && installed.length) {
    const scripts = path.join(installed[0].dir, 'scripts');
    step('MCP server handshake');
    const init = JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-06-18' } });
    const list = JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'tools/list' });
    const r = runPy(py, path.join(scripts, 'slr_mcp.py'), [], `${init}\n${list}\n`);
    const tools = (r.stdout.trim().split(/\r?\n/).map((l) => { try { return JSON.parse(l); } catch (_) { return {}; } })
      .find((m) => m.id === 2) || {}).result;
    if (tools && tools.tools) ok(`slr_mcp.py answers — tools: ${tools.tools.map((t) => t.name).join(', ')}`);
    else { fail(`slr_mcp.py did not answer: ${r.stderr.trim().split('\n').pop()}`); bad++; }

    step('API connection');
    const t = runPy(py, path.join(scripts, 'slr.py'), [...profileArgs(opts), 'config', 'test']);
    if (t.json && t.json.connected) ok(`profile ${t.json.profile}: project "${t.json.project}", ${t.json.assigned} assigned / ${t.json.completed} completed`);
    else { warn(`not connected: ${JSON.stringify((t.json && (t.json.error || t.json)) || t.stderr.trim())}`); bad++; }
  }
  console.log();
  if (bad) { fail(`${bad} problem(s) found`); process.exitCode = 1; } else ok('All good');
}

async function cmdUninstall(opts) {
  const targets = opts.target ? resolveTargets(opts) : findInstalledSkills();
  if (!(await confirm(`Remove /slr skill (${targets.map((t) => t.dir).join(', ') || 'none found'}) and MCP entries?`, opts))) return;
  for (const t of targets) {
    if (!fs.existsSync(t.dir)) continue;
    fs.rmSync(t.dir, { recursive: true, force: true });
    ok(`Removed ${t.dir}`);
  }
  try { if (unregisterClaudeCode()) ok('Claude Code MCP entry removed'); } catch (e) { warn(`Claude Code: ${e.message}`); }
  try { if (unregisterDesktop()) ok('Claude Desktop MCP entry removed'); } catch (e) { warn(`Claude Desktop: ${e.message}`); }
  const confDir = process.env.REVIEW_CONFIG_DIR || path.join(HOME, '.config', 'slr');
  if (opts.purge) {
    fs.rmSync(confDir, { recursive: true, force: true });
    ok(`Removed saved URLs/tokens: ${confDir}`);
  } else info(`Saved URLs/tokens kept in ${confDir} (use --purge to delete)`);
  info('Claude Science: remove the "slr" connector in Settings → Connectors if you added it.');
}

function repoSlug() {
  const m = ((PKG.repository && PKG.repository.url) || '').match(/github\.com[/:]([^/]+\/[^/.]+)/);
  return m ? m[1] : '<user>/<repo>';
}

// ------------------------------------------------------------------ main

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  switch (opts.cmd) {
    case 'install': case 'update': return cmdInstall(opts);
    case 'mcp': return cmdMcp(opts);
    case 'doctor': return cmdDoctor(opts);
    case 'uninstall': case 'remove': return cmdUninstall(opts);
    case 'version': return console.log(PKG.version);
    case 'help': return console.log(HELP);
    default: die(`Unknown command "${opts.cmd}" (see --help)`);
  }
}

main().catch((e) => die(e.stack || e.message));
