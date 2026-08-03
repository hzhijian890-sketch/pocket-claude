# 🚀 Feishu x Claude Code Bot

> Phone → Feishu → Claude Code on your PC. Chat with DeepSeek. Send files. Multi-step automation. All from one Python file.

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://python.org)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Lines](https://img.shields.io/badge/lines-1113-lightgrey.svg)](bot.py)

---

## ✨ 5 Commands

| Command | What it does |
|:---|:---|
| `!exec` / `!run` | Single Claude Code task, 300s timeout. Multi-turn via `--resume`. |
| `!auto` | Multi-step task. Claude plans → executes step-by-step → reports progress. |
| `!file` / `!send` | Upload a file to Feishu, send to your phone. Fuzzy search by keyword. |
| `!check` / `!ls` | Browse a directory. `!check 桌面`, reply `more` for next page. |
| `!status` | PC health: CPU, memory, bot uptime, active slots. |

Normal messages → DeepSeek V4 Flash chat.

## 🧠 Architecture

```
┌──────────┐     WebSocket      ┌──────────────┐     subprocess     ┌─────────────┐
│  Feishu  │ ◄──────────────► │   bot.py      │ ────────────────► │  Claude CLI  │
│ (phone)  │                   │               │                    │  (local PC)  │
└──────────┘                   │ ┌───────────┐ │                    └─────────────┘
                               │ │ DeepSeek   │ │
                               │ │ (chat)     │ │
                               │ └───────────┘ │
                               └──────────────┘
```

## 🚀 Quick Start

### Prerequisites

- Python 3.10+
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (`npm install -g @anthropic-ai/claude-code`)
- A [Feishu app](https://open.feishu.cn/app) with bot capability enabled

### 1. Clone & install

```bash
git clone https://github.com/05zhijian/pocket-claude.git
cd pocket-claude
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env:
#   FEISHU_APP_ID=cli_xxxxxxxx
#   FEISHU_APP_SECRET=xxxxxxxx
#   DEEPSEEK_API_KEY=sk-xxxxxxxx
```

### 3. Run

```bash
python bot.py
```

### 4. From your phone

```
!exec list files on desktop     → Claude Code
!auto 整理下载文件夹             → multi-step automation
!file 报销单                    → fuzzy search → send file
!check 桌面                     → browse directory
!status                         → PC health
```

## ⚙️ Configuration

All optional — defaults work.

| Variable | Default | Description |
|:---|:---|:---|
| `CLAUDE_PATH` | Auto-detected | Path to `claude` binary |
| `CLAUDE_TIMEOUT` | `300` | Max seconds per Claude task |
| `SESSION_IDLE_TIMEOUT` | `1800` | Session expiry (seconds) |
| `PERMISSION_MODE` | `bypassPermissions` | Claude permission level |
| `MAX_CONCURRENT_CLAUDE` | `3` | Max concurrent processes |

## 📁 Project

```
pocket-claude/
├── bot.py              # Main bot (1113 lines)
├── requirements.txt    # 3 dependencies
├── .env.example        # Config template
├── README.md
├── LICENSE
└── legacy/             # V1/V2 experiments
```

## 📄 License

MIT
