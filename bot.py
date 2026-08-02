"""
bot.py — Feishu x Claude Code bridge with multi-turn session support.

Architecture:
  Feishu message → handle_message
                     ├─ !exec → worker thread → claude -p --output-format json → reply
                     └─ normal  → DeepSeek chat API

Session continuity:
  First call:  claude -p --output-format json → returns session_id
  Next calls:  --resume <session_id> → same conversation, preserved context
  After 30 min idle: session_id cleared, next call starts fresh.

Usage:
  python bot.py
"""

import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid

import requests
from dotenv import load_dotenv
from lark_oapi import Client as LarkClient
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
from lark_oapi.core.enum import LogLevel
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
from lark_oapi.ws import Client as WsClient

# ============================================================
# Configuration
# ============================================================
load_dotenv()
APP_ID = os.getenv("FEISHU_APP_ID", "").strip()
APP_SECRET = os.getenv("FEISHU_APP_SECRET", "").strip()
DS_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-v4-flash"
EXEC_PREFIXES = ("!exec", "!run", "！exec", "！run", "exec:", "run:")

CLAUDE_EXE = os.getenv("CLAUDE_PATH", os.path.expandvars(
    r"%USERPROFILE%\AppData\Roaming\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe"
))
CLAUDE_TIMEOUT = int(os.getenv("CLAUDE_TIMEOUT", "300"))
SESSION_IDLE = int(os.getenv("SESSION_IDLE_TIMEOUT", "1800"))
PERMISSION_MODE = os.getenv("PERMISSION_MODE", "bypassPermissions")
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_CLAUDE", "3"))

# Global semaphore to limit concurrent Claude subprocesses
_claude_slots = threading.Semaphore(MAX_CONCURRENT)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("feishu-bot")


def _strip_exec_prefix(text: str) -> str:
    """Strip !exec / ！exec prefix, returning the plain task text."""
    for p in EXEC_PREFIXES:
        if text.startswith(p):
            return text[len(p):].strip()
    return text


# ============================================================
# DeepSeek API
# ============================================================
def ask_deepseek(text: str) -> str:
    headers = {
        "Authorization": f"Bearer {DS_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": "你是一个友好、专业的 AI 助手。"},
            {"role": "user", "content": text},
        ],
        "temperature": 0.7,
        "max_tokens": 4096,
    }
    try:
        res = requests.post(DEEPSEEK_URL, json=payload, headers=headers, timeout=60)
        if res.status_code == 200:
            return res.json()["choices"][0]["message"]["content"]
        logger.error(f"DeepSeek API error {res.status_code}: {res.text}")
        return f"❌ AI 接口异常（{res.status_code}），请稍后重试。"
    except requests.RequestException as e:
        logger.error(f"DeepSeek request failed: {e}")
        return f"❌ 请求 AI 失败：{e}"


# ============================================================
# Feishu Message API
# ============================================================
def send_message(open_id: str, text: str) -> None:
    client = (
        LarkClient.builder()
        .app_id(APP_ID)
        .app_secret(APP_SECRET)
        .build()
    )
    body = (
        CreateMessageRequestBody.builder()
        .receive_id(open_id)
        .msg_type("text")
        .content(json.dumps({"text": text}, ensure_ascii=False))
        .build()
    )
    req = (
        CreateMessageRequest.builder()
        .receive_id_type("open_id")
        .request_body(body)
        .build()
    )
    resp = client.im.v1.message.create(req)
    if not resp.success():
        logger.error(f"Send failed: code={resp.code}, msg={resp.msg}")


# ============================================================
# ClaudeSession — multi-turn via --output-format json + --resume
# ============================================================
class ClaudeSession:
    """
    Each call spawns an independent claude -p --output-format json process.
    Session continuity is achieved by passing --resume <session_id>.
    """

    def __init__(self, session_id: str):
        self.owner = session_id                     # Feishu open_id
        self._claude_session_id: str | None = None  # Claude-side session_id
        self._lock = threading.Lock()
        self.last_activity = time.time()

    def is_expired(self) -> bool:
        return (time.time() - self.last_activity) > SESSION_IDLE

    def cleanup(self) -> None:
        pass  # Stateless — each call is an independent process

    def send(self, text: str) -> str:
        with self._lock:
            self.last_activity = time.time()

            cmd = [
                CLAUDE_EXE,
                "-p",
                "--output-format", "json",
                "--permission-mode", PERMISSION_MODE,
            ]
            if self._claude_session_id:
                cmd += ["--resume", self._claude_session_id]

            logger.info(f"[{self.owner}] Running claude... (resume={bool(self._claude_session_id)})")

            try:
                p = subprocess.run(
                    cmd,
                    input=text,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=CLAUDE_TIMEOUT if CLAUDE_TIMEOUT > 0 else None,
                )
            except subprocess.TimeoutExpired:
                return f"❌ Claude 执行超时（>{CLAUDE_TIMEOUT} 秒）"
            except FileNotFoundError:
                return f"❌ 未找到: {CLAUDE_EXE}"
            except Exception as e:
                return f"❌ 子进程异常: {e}"

            if p.returncode != 0:
                logger.error(f"Claude exit={p.returncode} stderr={p.stderr[:300]}")
                # --resume session may have been pruned; clear and retry fresh next time
                if "No conversation found" in p.stderr or "session" in p.stderr.lower():
                    self._claude_session_id = None
                    logger.info(f"[{self.owner}] Session lost, will start fresh next time.")
                return f"❌ Claude 返回错误 (exit={p.returncode})"

            try:
                data = json.loads(p.stdout)
            except json.JSONDecodeError:
                logger.error(f"Claude output not JSON: {p.stdout[:300]}")
                return f"❌ Claude 输出解析失败"

            if data.get("is_error"):
                err = data.get("result", data.get("api_error_status", "unknown error"))
                return f"❌ Claude: {err}"

            # Save session_id for next --resume
            new_sid = data.get("session_id")
            if new_sid:
                self._claude_session_id = new_sid

            result = (data.get("result") or "").strip()
            if not result:
                return "(Claude 返回空内容)"

            return result


# ============================================================
# SessionManager — per-user session lifecycle
# ============================================================
class SessionManager:
    def __init__(self):
        self._sessions: dict[str, ClaudeSession] = {}
        self._lock = threading.Lock()

    def get(self, open_id: str) -> ClaudeSession:
        with self._lock:
            expired = [k for k, v in self._sessions.items() if v.is_expired()]
            for k in expired:
                logger.info(f"Session expired: {k}")
                self._sessions.pop(k, None)

            if open_id not in self._sessions:
                logger.info(f"New session: {open_id}")
                self._sessions[open_id] = ClaudeSession(open_id)
            return self._sessions[open_id]

    def shutdown(self):
        with self._lock:
            self._sessions.clear()


session_manager = SessionManager()


# ============================================================
# Background Task Execution
# ============================================================
def run_claude_task(open_id: str, task_id: str, user_text: str) -> None:
    """Execute Claude in a worker thread, reply via Feishu on completion."""
    start = time.time()
    clean_text = _strip_exec_prefix(user_text)
    logger.info(f"[{task_id}] >> {clean_text[:80]}...")

    acquired = _claude_slots.acquire(timeout=CLAUDE_TIMEOUT)
    if not acquired:
        logger.warning(f"[{task_id}] No Claude slot available, rejecting.")
        send_message(open_id, "❌ 当前 Claude 执行任务已满，请稍后重试。")
        return

    try:
        session = session_manager.get(open_id)
        result = session.send(clean_text)
    except Exception as e:
        logger.error(f"[{task_id}] Claude error: {e}")
        result = f"❌ Claude 执行异常: {e}"
    finally:
        _claude_slots.release()

    elapsed = time.time() - start
    logger.info(f"[{task_id}] Done ({elapsed:.1f}s)")

    if len(result) > 15000:
        result = result[:15000] + "\n\n…（内容过长已截断）"
    send_message(open_id, result)


# ============================================================
# Event Handler
# ============================================================
def handle_message(data) -> None:
    try:
        event = data.event
        message = event.message

        if message.chat_type != "p2p":
            return
        if message.message_type != "text":
            return

        raw = message.content
        try:
            content_obj = json.loads(raw)
        except json.JSONDecodeError:
            return
        user_text = (content_obj.get("text") or "").strip()
        if not user_text:
            return

        open_id = event.sender.sender_id.open_id
        logger.info(f"[{open_id}] >> {user_text}")

        if user_text.startswith(EXEC_PREFIXES):
            task_id = uuid.uuid4().hex[:8]
            send_message(open_id, f"⏳ Claude 正在处理…（{task_id}）")
            t = threading.Thread(
                target=run_claude_task,
                args=(open_id, task_id, user_text),
                daemon=True,
                name=f"task-{task_id}",
            )
            t.start()
            return

        reply = ask_deepseek(user_text)
        send_message(open_id, reply)
        logger.info(f"[{open_id}] << {reply[:100]}...")

    except Exception as e:
        logger.error(f"handle_message error: {e}", exc_info=True)
        try:
            open_id = data.event.sender.sender_id.open_id
            send_message(open_id, f"❌ 机器人内部错误：{e}")
        except Exception:
            pass


# ============================================================
# Entry Point
# ============================================================
def main() -> None:
    missing = []
    if not APP_ID: missing.append("FEISHU_APP_ID")
    if not APP_SECRET: missing.append("FEISHU_APP_SECRET")
    if not DS_KEY: missing.append("DEEPSEEK_API_KEY")
    if missing:
        logger.error(f"Missing config: {', '.join(missing)}. Check your .env file.")
        sys.exit(1)

    if not os.path.isfile(CLAUDE_EXE):
        logger.error(f"Claude CLI not found: {CLAUDE_EXE}")
        logger.error("Set the correct CLAUDE_PATH in .env")
        sys.exit(1)

    dispatcher = (
        EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(handle_message)
        .build()
    )

    ws = WsClient(
        app_id=APP_ID,
        app_secret=APP_SECRET,
        event_handler=dispatcher,
        log_level=LogLevel.INFO,
    )

    logger.info("=" * 50)
    logger.info("  Feishu x Claude Code Bot  v1.0")
    logger.info(f"  Claude:  {CLAUDE_EXE}")
    logger.info(f"  Prefix:  {', '.join(EXEC_PREFIXES)} -> claude -p --output-format json")
    logger.info(f"  Session: --resume multi-turn, {SESSION_IDLE}s idle expiry")
    logger.info(f"  Timeout: {CLAUDE_TIMEOUT}s")
    logger.info(f"  Perm:    --permission-mode {PERMISSION_MODE}")
    logger.info(f"  Slots:   max {MAX_CONCURRENT} concurrent Claude processes")
    logger.info("=" * 50)

    try:
        ws.start()
    finally:
        session_manager.shutdown()


if __name__ == "__main__":
    main()
