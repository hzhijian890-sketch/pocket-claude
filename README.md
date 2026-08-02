# 🚀 Feishu x Claude Code Bot

> Control Claude Code from your phone via Feishu. Chat with DeepSeek for casual conversation, execute tasks with Claude Code for real work.

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://python.org)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Lines](https://img.shields.io/badge/lines-370-lightgrey.svg)](bot.py)

---

## ✨ Why this exists

I wanted to drive Claude Code on my PC **from my phone** — kick off big refactors, generate files, debug issues — without sitting at the desk. Existing solutions were all TypeScript, hundreds of dependencies, and purely bridge-focused. This is **370 lines of Python** that also gives you an AI chat backend (DeepSeek) for everyday questions.

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

| Message type | Routed to | Description |
|:---|:---|:---|
| `!exec build a todo app` | **Claude Code** | Spawns `claude -p` subprocess, returns result |
| `!run ls -la` | **Claude Code** | Same as `!exec`, different prefix |
| `你好，解释一下 Python 装饰器` | **DeepSeek V4 Flash** | Fast, cheap chat for everyday questions |

### Session continuity

```
First !exec:   claude -p --output-format json → returns session_id "abc123"
Second !exec:  claude -p --output-format json --resume abc123 → SAME conversation
30 min idle:   session expires → fresh conversation next time
```

## 🚀 Quick Start

### Prerequisites

- Python 3.10+
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed (`npm install -g @anthropic-ai/claude-code`)
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
# Edit .env with your real credentials:
#   FEISHU_APP_ID=cli_xxxxxxxx
#   FEISHU_APP_SECRET=xxxxxxxx
#   DEEPSEEK_API_KEY=sk-xxxxxxxx
```

### 3. Run

```bash
python bot.py
```

### 4. Test

Open Feishu, message your bot privately:

```
你好              → DeepSeek replies
!exec list files on my desktop  → Claude Code executes and replies
!exec 再做一个同样的操作        → Same Claude session, remembers context
```

## ⚙️ Configuration

All optional — defaults work for most setups.

| Variable | Default | Description |
|:---|:---|:---|
| `CLAUDE_PATH` | Auto-detected | Path to `claude.exe` / `claude` binary |
| `CLAUDE_TIMEOUT` | `300` | Max seconds per Claude task |
| `SESSION_IDLE_TIMEOUT` | `1800` | Seconds before session expires (30 min) |
| `PERMISSION_MODE` | `bypassPermissions` | `bypassPermissions` \| `acceptEdits` \| `default` |
| `MAX_CONCURRENT_CLAUDE` | `3` | Max Claude processes running at once |

## 📁 Project Structure

```
pocket-claude/
├── bot.py              # Main bot (370 lines)
├── requirements.txt    # Python dependencies
├── .env.example        # Config template
├── LICENSE             # MIT
└── legacy/             # V1 (file queue) & V2 (subprocess without sessions)
    ├── bot_v1_queue.py
    ├── bot_v2_subprocess.py
    └── agent_v1.py
```

## 🔄 Compared to similar projects

| Feature | This bot | lark-coding-agent-bridge | remote-claude | lark-claude-bridge |
|:---|:---:|:---:|:---:|:---:|
| Language | **Python** | TypeScript | TypeScript | Shell/TS |
| Lines | **370** | ~2000+ | ~1500+ | ~1000+ |
| Dual AI backend | ✅ | ❌ | ❌ | ❌ |
| Multi-turn sessions | ✅ | ✅ | ✅ | ✅ |
| Streaming output | ❌ | ✅ | ✅ | ✅ |
| Interactive cards | ❌ | ✅ | ✅ | ✅ |
| Dependencies | **3** | 100+ | 100+ | 50+ |

**Trade-off:** This bot is deliberately minimal — no streaming, no cards, no Docker. If you need those, check out the alternatives above. If you want a **simple, auditable, Python-native** bridge that just works, this is for you.

## 🗺️ Roadmap

- [ ] **v1.1** — Streaming output (Feishu interactive cards)
- [ ] **v1.2** — Permission confirmation via card buttons
- [ ] **v1.3** — systemd / launchd / Task Scheduler daemon mode
- [ ] **v1.4** — Multi-workspace support
- [ ] **v1.5** — macOS / Linux verification pass

## 📄 License

MIT — do whatever you want with it.
