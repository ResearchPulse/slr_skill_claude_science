<div align="center">

# 📚 /slr — Systematic Literature Review Skill

**Trợ lý sàng lọc tài liệu cho Claude Code · Claude Science · Claude Desktop**

Lấy task từ Agent Task API → đọc metadata & full text → chấm theo tiêu chí inclusion/exclusion →
soạn recommendation theo stage (`ADVANCE_TO_FULL_TEXT` ở Round 1; `INCLUDE` ở Round 2) cùng
`EXCLUDE` / `UNCERTAIN` và dẫn chứng → chỉ gửi khi nhà khoa học duyệt.

[![Node](https://img.shields.io/badge/node-%E2%89%A516.7-339933?logo=node.js&logoColor=white)](https://nodejs.org)
[![Python](https://img.shields.io/badge/python-%E2%89%A53.8-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![MCP](https://img.shields.io/badge/MCP-stdio-6E56CF)](https://modelcontextprotocol.io)
[![Zero deps](https://img.shields.io/badge/dependencies-0-brightgreen)](#-cấu-trúc-repo)

```bash
npx github:ResearchPulse/slr_skill_claude_science
```

*Một lệnh: cài skill + lưu URL/token + đăng ký MCP server.*

</div>

---

## 📑 Mục lục

- [Tính năng](#-tính-năng)
- [Yêu cầu](#-yêu-cầu)
- [Cài đặt](#-cài-đặt)
- [Trình cài đặt làm gì?](#️-trình-cài-đặt-làm-gì)
- [Kết nối MCP với Claude Science](#-kết-nối-mcp-với-claude-science)
- [Lệnh của trình cài đặt](#-lệnh-của-trình-cài-đặt)
- [Sử dụng trong Claude](#-sử-dụng-trong-claude)
- [Xử lý sự cố](#-xử-lý-sự-cố)
- [Cấu trúc repo](#-cấu-trúc-repo)

---

## ✨ Tính năng

- 🔎 **Sàng lọc tự động** từng bài theo IC/EC của dự án, mỗi nhận định đều trích dẫn section của bài.
- ✍️ **Draft trước, gửi sau** — phiếu chỉ được gửi khi bạn đồng ý; có `audit.jsonl` làm nhật ký kiểm toán.
- 🔌 **MCP connector** cho screening, Phase 2 extraction và leader consensus (`list_consensus_tasks`, `analyze_extraction_consensus`, `save_extraction_consensus_draft`, `submit_extraction_consensus` cùng các tool đọc/ghi liên quan) — vẫn hoạt động khi sandbox không có mạng.
- 🗂️ **Nhiều dự án** — mỗi dự án một profile, chuyển qua lại bằng `/slr config use`.
- 🪶 **Không phụ thuộc** — Python thư viện chuẩn, installer Node không cần `npm install`.

---

## 🧰 Yêu cầu

| Thành phần | Phiên bản | Ghi chú |
| --- | --- | --- |
| **Node.js** | ≥ 16.7 | chỉ để chạy lệnh cài đặt `npx` |
| **Python** | ≥ 3.8 | không cần `pip install` gì |
| **Claude** | Code / Science / Desktop | ít nhất một ứng dụng |

> [!TIP]
> Trên Windows, khi cài Python hãy tick **"Add python.exe to PATH"** (hoặc dùng `py` launcher có sẵn).

---

## 🚀 Cài đặt

### Cách 1 — `npx` (khuyên dùng)

```bash
npx github:ResearchPulse/slr_skill_claude_science
```

Trình cài đặt hỏi 2 thông tin rồi tự làm phần còn lại:

1. **API base URL** — ví dụ `http://localhost:5000/api` (URL nhìn từ máy của bạn)
2. **Agent token** — nhập sẽ bị ẩn; Enter để bỏ qua và đặt sau bằng `/slr config token`

### Cách 2 — Không tương tác (script / CI / nhiều máy)

<details open>
<summary><b>macOS / Linux / Git Bash</b></summary>

```bash
npx github:ResearchPulse/slr_skill_claude_science install \
  --url http://localhost:5000/api \
  --token <AGENT_TOKEN> \
  -y
```

</details>

<details>
<summary><b>Windows PowerShell</b></summary>

```powershell
npx github:ResearchPulse/slr_skill_claude_science install --url http://localhost:5000/api --token <AGENT_TOKEN> -y
```

</details>

Cũng có thể dùng biến môi trường thay cho cờ: `SLR_API_URL`, `SLR_TOKEN`.

### Cách 3 — Cài toàn cục bằng npm

```bash
npm install -g github:ResearchPulse/slr_skill_claude_science
slr-skill           # chạy trình cài đặt
slr-skill doctor    # kiểm tra
```

### 🔄 Cập nhật

Chỉ cần chạy lại lệnh cài đặt — skill được ghi đè bằng bản mới, **URL/token đã lưu được giữ nguyên**.

```bash
npx github:ResearchPulse/slr_skill_claude_science#main
```

---

## ⚙️ Trình cài đặt làm gì?

| Bước | Chi tiết |
| --- | --- |
| **1. Python** | Tự tìm `py -3` / `python3` / `python` (≥ 3.8). MCP dùng **đường dẫn tuyệt đối** tới `python.exe` nên app GUI không phụ thuộc PATH. |
| **2. Skill** | Copy `SKILL.md`, `scripts/`, `references/` vào<br>• Claude Code → `~/.claude/skills/slr`<br>• Claude Science → `~/.claude-science/skills/slr` *(nếu đã có `~/.claude-science`)* |
| **3. Cấu hình** | `slr.py config set-url` + `config set-token --stdin` → lưu ở `~/.config/slr/` (token quyền 0600), rồi test kết nối. |
| **4. MCP** | Đăng ký server `slr` (chạy `scripts/slr_mcp.py`)<br>• Claude Code → `claude mcp add -s user` *(fallback: `~/.claude.json`)*<br>• Claude Desktop → `claude_desktop_config.json` *(tạo bản `.bak`)*<br>• Claude Science → in JSON + lưu `<skill>/mcp-config.json` |

> [!NOTE]
> Mặc định **token không nằm trong config MCP** — server tự đọc profile ở `~/.config/slr`.
> Muốn ghi `API_BASE_URL` / `AGENT_TOKEN` vào `env` của MCP, thêm cờ `--token-in-env`.

---

## 🔌 Kết nối MCP với Claude Science

Claude Science cần thêm connector thủ công **một lần**:

1. Mở **Settings → Connectors / MCP servers → Add**
2. Dán JSON mà trình cài đặt in ra (hoặc in lại bằng `npx github:ResearchPulse/slr_skill_claude_science mcp`):

```json
{
  "mcpServers": {
    "slr": {
      "command": "C:\\Users\\<user>\\AppData\\Local\\Programs\\Python\\Python312\\python.exe",
      "args": ["C:\\Users\\<user>\\.claude-science\\skills\\slr\\scripts\\slr_mcp.py"]
    }
  }
}
```

3. Trong chat gõ `/slr mcp` → Claude gọi `connection_test` và báo `connected: true`.

---

## 🛠️ Lệnh của trình cài đặt

```text
npx github:ResearchPulse/slr_skill_claude_science [lệnh] [tuỳ chọn]
```

| Lệnh | Mô tả |
| --- | --- |
| `install` *(mặc định)* | Cài / cập nhật skill, cấu hình URL + token, đăng ký MCP |
| `mcp` | In JSON cấu hình MCP |
| `doctor` | Kiểm tra Python, skill, MCP entry, bắt tay MCP server, kết nối API |
| `uninstall` | Gỡ skill + MCP entry (`--purge` xoá luôn URL/token đã lưu) |

| Tuỳ chọn | Ý nghĩa |
| --- | --- |
| `--url <url>` | API base URL (tự thêm `/api` nếu thiếu) |
| `--token <tok>` | Agent token của dự án |
| `--profile <p>` | Lưu vào profile riêng — mỗi dự án một profile |
| `--target <t>` | Nơi cài: `code`, `science`, `all` hoặc đường dẫn thư mục (phân cách bằng dấu phẩy) |
| `--mcp <list>` | Client cần đăng ký: `code`, `desktop`, `none` (mặc định: mọi client tìm thấy) |
| `--no-mcp` | Bỏ qua bước MCP |
| `--token-in-env` | Ghi URL/token vào `env` của config MCP |
| `--python <cmd>` | Chỉ định trình thông dịch Python |
| `-y`, `--yes` | Không hỏi gì |

**Ví dụ**

```bash
# Chỉ cài cho Claude Science
npx github:ResearchPulse/slr_skill_claude_science --target science --mcp none

# Thêm dự án thứ hai vào profile riêng
npx github:ResearchPulse/slr_skill_claude_science --profile project-b \
  --url https://review.example.org/api --token <TOKEN> -y

# Gỡ hoàn toàn
npx github:ResearchPulse/slr_skill_claude_science uninstall --purge
```

---

## 💬 Sử dụng trong Claude

Mở Claude (khởi động lại Claude Desktop nếu đang chạy) rồi gõ `/slr setup` hoặc `/slr status`.

| Lệnh | Tác dụng |
| --- | --- |
| `/slr setup` | Thiết lập có hướng dẫn |
| `/slr status` | Profile, dự án, số task được giao / đã xong, số draft |
| `/slr list` | Danh sách task |
| `/slr review [N \| id…]` | Soạn screening hoặc extraction draft theo template, luôn hiện bảng field/evidence/check |
| `/slr drafts` | Bảng draft để duyệt |
| `/slr edit <id> <yêu cầu>` | Sửa một draft — vd. *"đổi sang UNCERTAIN, IC3 chưa rõ"* |
| `/slr submit [id…]` | Gửi phiếu đã duyệt qua MCP `submit_result` |
| `/slr auto [N]` | Soạn và gửi ngay (xác nhận một lần) |
| `/slr config …` | `show` · `url` · `token` · `use` · `test` · `remove` |
| `/slr consensus [task_id]` | Leader: tự tìm task sau khi R1/R2 submit, so sánh field/evidence, lưu draft và submit consensus chính thức khi không conflict |
| `/slr mcp` | Hiện config MCP / kiểm tra connector |
| `/slr diag` | Chẩn đoán proxy / CA / DNS |
| `/slr help` | Bảng lệnh |

> [!WARNING]
> Phiếu đã gửi là **cuối cùng** — API từ chối gửi lại (409). Luôn xem `/slr drafts` trước khi `/slr submit`.

**Quy trình điển hình**

```text
/slr setup  →  /slr review 10  →  /slr drafts  →  /slr edit <id> …  →  /slr submit
```

File làm việc nằm trong `./paper-review/` (hoặc `$REVIEW_WORKDIR`):
`fulltext/` · `drafts/` · `submitted/` · `audit.jsonl` — hãy giữ lại làm nhật ký kiểm toán.

---

## 🩺 Xử lý sự cố

Chạy trước tiên:

```bash
npx github:ResearchPulse/slr_skill_claude_science doctor
```

| Triệu chứng | Cách xử lý |
| --- | --- |
| `Python 3.8+ not found` | Cài Python, mở terminal mới; hoặc `--python "C:\path\to\python.exe"` |
| `NETWORK` / `actively refused` | Backend chưa chạy hoặc sai URL/port. Backend trong Docker → phải publish port |
| `401` | Token sai/đã đổi → chạy lại với `--token` hoặc `/slr config token` |
| Claude Code không thấy MCP | `claude mcp list`; cài lại với `--mcp code` |
| Claude Desktop không thấy MCP | Thoát hẳn app (cả icon khay hệ thống) rồi mở lại |
| Claude Science báo mất mạng | Bình thường — skill tự chuyển sang MCP connector; kiểm tra đã thêm connector |
| Không thấy lệnh `/slr` | `doctor` để kiểm tra `SKILL.md` đã có trong thư mục skill; khởi động lại Claude |

---

## 📁 Cấu trúc repo

```text
slr_skill_claude_science/
├── SKILL.md                     # định nghĩa skill /slr
├── scripts/
│   ├── slr.py                   # CLI + API client (stdlib)
│   └── slr_mcp.py               # MCP stdio server
├── references/
│   └── review-rubric.md         # rubric chấm theo tiêu chí
├── bin/
│   └── install.js               # trình cài đặt (npx / npm bin), không dependency
├── package.json
└── README.md
```

## Cập nhật Agent Task API

Skill và MCP tuân theo `AI_AGENT_TASK_API_CONTRACT.md`: agent token được giới hạn theo agent, project và reviewer workspace; danh sách task được lấy qua cursor; mỗi task dùng `protocolSnapshot` để chấm theo bộ tiêu chí tại thời điểm giao.

Chỉ lấy Markdown khi task có `stage: FULL_TEXT` và có `markdownPath`, đồng thời chỉ review khi source revision đã `COMPLETED`. Task `TITLE_ABSTRACT` dùng title, abstract và keywords nếu có; `keywords: null` là chưa có metadata, không phải lý do loại bài. MCP kiểm tra stage và decision theo từng vòng; `INCLUDE` bị từ chối ở Round 1 và không được đổi thành `ADVANCE_TO_FULL_TEXT`. Trạng thái trích xuất `202` tuân theo `Retry-After`; Markdown `PARTIAL` chưa dùng để review, cần đợi source revision `COMPLETED`.

Kết quả agent là recommendation riêng, không tạo screening decision của reviewer. Reviewer lấy/cấp lại token trong review workspace; nếu token bị rotate, cập nhật lại token trong cấu hình MCP và profile `/slr`.

<div align="center">
<sub>Made for <b>ResearchPulse</b> · Phát hiện lỗi? <a href="https://github.com/ResearchPulse/slr_skill_claude_science/issues">Mở issue</a></sub>
</div>

## Phase 3 Quality Assessment (`/slr qa`)

The Claude connector uses the same canonical backend Quality Assessment
workflow as the Open-Science connector. Reviewer-scoped tokens are limited to
the caller's assigned paper and own review; project-scoped leader tokens can
compare both submitted reviews and submit the official resolution.

| Command | Behavior |
| --- | --- |
| `/slr qa` | Show role, phase, strategy, assignments, and reviewer coverage |
| `/slr qa list` | List QA papers visible to the active token |
| `/slr qa review <paper_id>` | Read Cloudinary Markdown, score every criterion, attach exact quote/source-hash evidence, and save a draft |
| `/slr qa check <paper_id>` | Run deterministic value, completeness, and source-binding checks |
| `/slr qa submit <paper_id>` | Submit the reviewer's checked assessment after explicit approval |
| `/slr qa consensus <paper_id>` | Leader-only comparison of every criterion, both reviews, checks, and evidence |
| `/slr qa resolve <paper_id>` | Leader-only official High/Low resolution after explicit approval |
| `/slr qa complete` | Complete QA only after every eligible paper has two valid reviews and a resolution |

Exact quote, source revision, source hash, offsets, and quote hash are the
canonical evidence. `pdfHighlightCoordinates` is retained only for UI
compatibility. Reviewer output remains blind to the other reviewer; leader
output always includes the complete criterion/evidence tables. Drafts and
recommendations are never reported as official submission or completion.
