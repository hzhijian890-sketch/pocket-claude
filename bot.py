"""
bot.py — Feishu x Claude Code bridge with multi-turn session support.

Architecture:
  Feishu message → handle_message
                     ├─ !file → worker thread → upload file to Feishu → file message
                     ├─ !exec → worker thread → claude -p --output-format json → reply
                     └─ normal  → DeepSeek chat API

Session continuity:
  First call:  claude -p --output-format json → returns session_id
  Next calls:  --resume <session_id> → same conversation, preserved context
  After 30 min idle: session_id cleared, next call starts fresh.

File transfer:
  !file C:\\path\\to\\report.pdf  →  uploads the file to Feishu, sends to your phone
  Aliases: !send, !文件, file:, send:

Usage:
  python bot.py
"""

import json
import logging
import os
import re
try:
    import ctypes
    from ctypes import wintypes, byref, Structure, c_uint64, c_uint32, sizeof
except ImportError:
    ctypes = None  # non-Windows platform
import subprocess
import sys
import threading
import time
import uuid

import requests
from dotenv import load_dotenv
from lark_oapi import Client as LarkClient
from lark_oapi.api.im.v1 import (
    CreateFileRequest,
    CreateFileRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
)
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
FILE_PREFIXES = ("!file", "!send", "！file", "！send", "!文件", "！文件", "file:", "send:")
AUTO_PREFIXES = ("!auto", "！auto", "auto:")
STATUS_PREFIXES = ("!status", "！status", "status:")
CHECK_PREFIXES = ("!check", "！check", "!ls", "！ls", "check:", "ls:")
FEISHU_FILE_LIMIT = 20 * 1024 * 1024  # 20 MB

CLAUDE_EXE = os.getenv("CLAUDE_PATH", os.path.expandvars(
    r"%USERPROFILE%\AppData\Roaming\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe"
))
CLAUDE_TIMEOUT = int(os.getenv("CLAUDE_TIMEOUT", "300"))
SESSION_IDLE = int(os.getenv("SESSION_IDLE_TIMEOUT", "1800"))
PERMISSION_MODE = os.getenv("PERMISSION_MODE", "bypassPermissions")
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_CLAUDE", "3"))

# File search configuration
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
SEARCH_ROOTS = [
    ("Desktop",   os.path.expanduser("~/Desktop")),
    ("Downloads", os.path.expanduser("~/Downloads")),
    ("Documents", os.path.expanduser("~/Documents")),
    ("Project",   PROJECT_DIR),
]
SEARCH_MAX_RESULTS = 10
SEARCH_MAX_DEPTH = 4
SELECTION_EXPIRY = 60  # seconds

# Global semaphore to limit concurrent Claude subprocesses
_claude_slots = threading.Semaphore(MAX_CONCURRENT)

# Pending file selection cache: open_id -> (expiry_ts, [file_paths])
_pending_selections: dict[str, tuple[float, list[str]]] = {}
_pending_lock = threading.Lock()

# Pending directory listing cache: open_id -> (expiry_ts, dir_path, all_items, offset)
_pending_checks: dict[str, tuple[float, str, list[tuple[str, bool, int, float]], int]] = {}

# Bot start time (for uptime display)
BOT_START_TIME = time.time()

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
# File Search — fuzzy search local filesystem when path is unknown
# ============================================================
def _search_files(keyword: str) -> list[str]:
    """Search common directories for files whose name contains keyword."""
    keyword_lower = keyword.lower()
    seen: set[str] = set()
    scored: list[tuple[float, str]] = []  # (mtime, path)

    for _label, root in SEARCH_ROOTS:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            # Limit depth
            rel_depth = dirpath[len(root):].count(os.sep)
            if rel_depth >= SEARCH_MAX_DEPTH:
                dirnames.clear()
                continue
            # Skip hidden and noise directories
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".")
                and d not in ("node_modules", "__pycache__", ".git", "venv", ".venv",
                              "env", "site-packages", "Program Files", "Windows",
                              "AppData", "$RECYCLE.BIN")
            ]
            for fname in filenames:
                if keyword_lower in fname.lower():
                    full = os.path.join(dirpath, fname)
                    if full not in seen:
                        seen.add(full)
                        try:
                            mtime = os.path.getmtime(full)
                        except OSError:
                            mtime = 0
                        scored.append((mtime, full))

    # Most-recently-modified first
    scored.sort(key=lambda x: x[0], reverse=True)
    return [path for _mtime, path in scored[:SEARCH_MAX_RESULTS]]


# ============================================================
# File Transfer — upload file to Feishu, send as file message
# ============================================================
def _strip_file_prefix(text: str) -> str:
    """Strip !file / !send / ！file prefix, returning the file path."""
    for p in FILE_PREFIXES:
        if text.startswith(p):
            return text[len(p):].strip()
    return text


def _strip_auto_prefix(text: str) -> str:
    """Strip !auto / auto: prefix, returning the task description."""
    for p in AUTO_PREFIXES:
        if text.startswith(p):
            return text[len(p):].strip()
    return text


def send_file_message(open_id: str, file_path: str) -> None:
    """Upload a local file to Feishu and send it as a file message."""
    client = (
        LarkClient.builder()
        .app_id(APP_ID)
        .app_secret(APP_SECRET)
        .build()
    )

    file_name = os.path.basename(file_path)
    _, ext = os.path.splitext(file_name)
    ext = ext.lower().lstrip(".")

    # Map extension to Feishu file_type
    file_type_map = {
        "pdf": "pdf", "doc": "doc", "docx": "doc",
        "xls": "xls", "xlsx": "xls", "ppt": "ppt", "pptx": "ppt",
        "csv": "csv", "txt": "stream", "log": "stream",
        "py": "stream", "js": "stream", "ts": "stream",
        "json": "stream", "xml": "stream", "yaml": "stream", "yml": "stream",
        "md": "stream", "html": "stream", "css": "stream",
        "sh": "stream", "bat": "stream", "ps1": "stream",
        "c": "stream", "cpp": "stream", "h": "stream",
        "java": "stream", "rs": "stream", "go": "stream",
        "png": "image", "jpg": "image", "jpeg": "image",
        "gif": "image", "bmp": "image", "svg": "image", "webp": "image",
        "zip": "stream", "tar": "stream", "gz": "stream", "7z": "stream",
        "mp3": "stream", "mp4": "stream", "wav": "stream",
    }
    file_type = file_type_map.get(ext, "stream")

    # Step 1: Upload the file to Feishu
    logger.info(f"[{open_id}] Uploading file: {file_path} (type={file_type})")
    try:
        with open(file_path, "rb") as f:
            upload_body = (
                CreateFileRequestBody.builder()
                .file_type(file_type)
                .file_name(file_name)
                .file(f)
                .build()
            )
            upload_req = CreateFileRequest.builder().request_body(upload_body).build()
            upload_resp = client.im.v1.file.create(upload_req)
    except FileNotFoundError:
        send_message(open_id, f"❌ 文件不存在：{file_path}")
        return
    except PermissionError:
        send_message(open_id, f"❌ 无权限读取文件：{file_path}")
        return
    except Exception as e:
        logger.error(f"Upload error: {e}")
        send_message(open_id, f"❌ 上传文件失败：{e}")
        return

    if not upload_resp.success():
        logger.error(f"Upload failed: code={upload_resp.code}, msg={upload_resp.msg}")
        send_message(open_id, f"❌ 飞书上传失败 (code={upload_resp.code}): {upload_resp.msg}")
        return

    file_key = upload_resp.data.file_key
    logger.info(f"[{open_id}] Uploaded → file_key={file_key}")

    # Step 2: Send as file message
    msg_body = (
        CreateMessageRequestBody.builder()
        .receive_id(open_id)
        .msg_type("file")
        .content(json.dumps({"file_key": file_key}, ensure_ascii=False))
        .build()
    )
    msg_req = (
        CreateMessageRequest.builder()
        .receive_id_type("open_id")
        .request_body(msg_body)
        .build()
    )
    msg_resp = client.im.v1.message.create(msg_req)
    if not msg_resp.success():
        logger.error(f"Send file failed: code={msg_resp.code}, msg={msg_resp.msg}")
        send_message(open_id, f"❌ 发送文件失败 (code={msg_resp.code}): {msg_resp.msg}")


def run_file_transfer(open_id: str, user_text: str) -> None:
    """Validate path and transfer file to Feishu."""
    clean_path = _strip_file_prefix(user_text)

    # Strip surrounding quotes if present
    if clean_path and clean_path[0] in ('"', "'") and clean_path[-1] in ('"', "'"):
        clean_path = clean_path[1:-1]

    if not clean_path:
        send_message(open_id, "❌ 请指定文件路径。用法：!file C:\\path\\to\\file.pdf")
        return

    # Resolve path (handle ~ and relative paths)
    file_path = os.path.expandvars(os.path.expanduser(clean_path))
    if not os.path.isabs(file_path):
        file_path = os.path.abspath(file_path)
    file_path = os.path.normpath(file_path)

    if not os.path.isfile(file_path):
        # Not an exact path — fuzzy search by filename keyword
        keyword = os.path.basename(clean_path.rstrip("\\/"))
        if not keyword:
            send_message(open_id, "❌ 请提供文件路径或文件名关键词。用法：!file 报销单")
            return

        matches = _search_files(keyword)
        if not matches:
            send_message(open_id,
                f"❌ 未找到匹配「{keyword}」的文件。\n"
                f"搜索范围：桌面、下载、文档、Bot 项目目录（深度 ≤{SEARCH_MAX_DEPTH} 层）\n"
                f"如需精确路径，请提供完整路径如 !file C:\\Users\\H\\Desktop\\file.pdf")
            return

        if len(matches) == 1:
            # Single match — send directly
            file_path = matches[0]
            file_size = os.path.getsize(file_path)
            size_display = (
                f"{file_size / 1024:.1f} KB" if file_size < 1024 * 1024
                else f"{file_size / (1024 * 1024):.1f} MB"
            )
            send_message(open_id,
                f"🔍 找到：{os.path.basename(file_path)} ({size_display})\n"
                f"📤 正在上传…")
            send_file_message(open_id, file_path)
            return

        # Multiple matches — cache and present options
        expiry = time.time() + SELECTION_EXPIRY
        with _pending_lock:
            _pending_selections[open_id] = (expiry, matches)

        lines = [f'🔍 找到 {len(matches)} 个匹配「{keyword}」的文件：']
        for i, path in enumerate(matches, 1):
            size = os.path.getsize(path)
            size_display = (
                f"{size / 1024:.0f}KB" if size < 1024 * 1024
                else f"{size / (1024 * 1024):.1f}MB"
            )
            lines.append(f"  {i}. {os.path.basename(path)}  ({size_display})\n     {path}")
        lines.append(f"\n回复数字序号即可发送对应文件（{SELECTION_EXPIRY}s 内有效）。")
        send_message(open_id, "\n".join(lines))
        return

    file_size = os.path.getsize(file_path)
    if file_size > FEISHU_FILE_LIMIT:
        size_mb = file_size / (1024 * 1024)
        limit_mb = FEISHU_FILE_LIMIT / (1024 * 1024)
        send_message(open_id,
            f"❌ 文件过大（{size_mb:.1f} MB），飞书限制单文件 ≤ {limit_mb:.0f} MB。\n"
            f"路径：{file_path}")
        return

    size_display = f"{file_size / 1024:.1f} KB" if file_size < 1024 * 1024 else f"{file_size / (1024 * 1024):.1f} MB"
    send_message(open_id, f"📤 正在上传：{os.path.basename(file_path)} ({size_display})…")
    send_file_message(open_id, file_path)


# ============================================================
# Auto Task — multi-step execution with progress updates
# ============================================================
def _parse_plan_json(text: str) -> list[str] | None:
    """Extract a JSON array of strings from Claude's output."""
    # Direct parse
    try:
        result = json.loads(text)
        if isinstance(result, list) and all(isinstance(s, str) for s in result):
            return result
    except json.JSONDecodeError:
        pass
    # Markdown code block
    m = re.search(r'```(?:json)?\s*(\[.*?\])\s*```', text, re.DOTALL)
    if m:
        try:
            result = json.loads(m.group(1))
            if isinstance(result, list) and all(isinstance(s, str) for s in result):
                return result
        except json.JSONDecodeError:
            pass
    # Any JSON array in the text
    m = re.search(r'\[.*\]', text, re.DOTALL)
    if m:
        try:
            result = json.loads(m.group(0))
            if isinstance(result, list) and all(isinstance(s, str) for s in result):
                return result
        except json.JSONDecodeError:
            pass
    return None


def run_auto_task(open_id: str, user_text: str) -> None:
    """Decompose a task, execute step by step, report progress to Feishu."""
    clean = _strip_auto_prefix(user_text)
    if not clean:
        send_message(open_id, "❌ 请描述任务。用法：!auto 整理下载文件夹中的文件按类型分类")
        return

    # ── Phase 1: Generate plan ──
    send_message(open_id, "🔧 Claude 正在分析任务并制定执行计划…")

    plan_prompt = (
        "You are a task planner. Decompose the following task into concrete executable steps.\n\n"
        f"TASK: {clean}\n\n"
        "Constraints:\n"
        "- Each step must be completable in a single Claude Code invocation (you have full tool access).\n"
        "- Be specific: mention file names, commands, and expected outcomes.\n"
        "- 3 to 8 steps maximum.\n"
        "- Steps must be ordered logically: each step builds on all previous steps.\n"
        "- The LAST step should verify that everything works correctly.\n\n"
        "Return ONLY a JSON array of strings. No markdown, no wrapper object, no extra text.\n"
        'Format: ["Step 1: do X", "Step 2: do Y", "Step 3: verify everything works"]'
    )

    # Planning doesn't use session — independent Claude call
    acquired = _claude_slots.acquire(timeout=CLAUDE_TIMEOUT)
    if not acquired:
        send_message(open_id, "❌ Claude 执行槽已满，请稍后重试。")
        return
    try:
        p = subprocess.run(
            [CLAUDE_EXE, "-p", "--output-format", "json",
             "--permission-mode", PERMISSION_MODE],
            input=plan_prompt,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=CLAUDE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        send_message(open_id, "❌ 计划生成超时，请尝试更简单的任务描述。")
        return
    except Exception as e:
        send_message(open_id, f"❌ 计划生成失败: {e}")
        return
    finally:
        _claude_slots.release()

    if p.returncode != 0:
        logger.error(f"Plan gen exit={p.returncode}: {p.stderr[:200]}")
        send_message(open_id, f"❌ 计划生成失败 (exit={p.returncode})，请重试。")
        return

    try:
        data = json.loads(p.stdout)
    except json.JSONDecodeError:
        send_message(open_id, "❌ 计划解析失败，请重试。")
        return

    plan_text = (data.get("result") or "").strip()
    steps = _parse_plan_json(plan_text)
    if not steps:
        send_message(open_id,
            f"❌ 无法从 Claude 输出中提取执行计划。\n原始输出（前 400 字符）:\n{plan_text[:400]}")
        return

    total_est = len(steps) * CLAUDE_TIMEOUT // 60
    lines = [f"📋 执行计划 — 共 {len(steps)} 步（每步 ≤{CLAUDE_TIMEOUT}s，预估 {total_est} 分钟）："]
    for i, step in enumerate(steps, 1):
        lines.append(f"  {i}. {step}")
    send_message(open_id, "\n".join(lines))

    # ── Phase 2: Execute steps sequentially ──
    session = session_manager.get(open_id)
    step_results: list[str] = []

    for i, step in enumerate(steps, 1):
        # Acquire a slot for this step
        acquired = _claude_slots.acquire(timeout=CLAUDE_TIMEOUT + 30)
        if not acquired:
            send_message(open_id,
                f"⛔ Step {i}/{len(steps)} 获取执行槽超时。"
                f"剩余 {len(steps) - i} 步未执行。")
            return

        send_message(open_id, f"⏳ Step {i}/{len(steps)}: {step[:60]}…")

        try:
            exec_prompt = (
                f"EXECUTE THIS SPECIFIC STEP ({i} of {len(steps)}):\n"
                f"{step}\n\n"
                f"CRITICAL INSTRUCTIONS:\n"
                f"- Focus ONLY on this step. Do NOT try to do everything at once.\n"
                f"- Previous steps have been completed — their results exist on disk.\n"
                f"- Be thorough: read/write files, run commands as needed.\n"
                f"- If you hit errors, try to fix them before giving up.\n"
                f"- After completing, summarize what you did in 2-3 sentences."
            )
            result = session.send(exec_prompt)
            step_results.append(result)
        except Exception as e:
            logger.error(f"Step {i} exception: {e}")
            send_message(open_id, f"❌ Step {i}/{len(steps)} 异常: {e}")
            send_message(open_id,
                f"⛔ 已停止。剩余 {len(steps) - i} 步未执行。"
                f"可用 !exec 手动继续。")
            return
        finally:
            _claude_slots.release()

        if result.startswith("❌"):
            truncated = result[:400] + ("…" if len(result) > 400 else "")
            send_message(open_id, f"❌ Step {i}/{len(steps)} 失败:\n{truncated}")
            send_message(open_id,
                f"⛔ 已停止。剩余 {len(steps) - i} 步未执行。"
                f"可用 !exec 手动继续。")
            return

        truncated = result[:300] + ("…" if len(result) > 300 else "")
        send_message(open_id, f"✅ Step {i}/{len(steps)} 完成:\n{truncated}")

    # ── Done ──
    elapsed = (
        f"整个流程共 {len(steps)} 步完成。\n"
        f"📁 如需将生成的文件传到手机，请使用 !file <文件名> 搜索。"
    )
    send_message(open_id, f"🎉 {elapsed}")


# ============================================================
# System Status
# ============================================================
class _FILETIME(Structure):
    _fields_ = [("dwLowDateTime", c_uint32), ("dwHighDateTime", c_uint32)]


class _MEMORYSTATUSEX(Structure):
    _fields_ = [
        ("dwLength", c_uint32),
        ("dwMemoryLoad", c_uint32),
        ("ullTotalPhys", c_uint64),
        ("ullAvailPhys", c_uint64),
        ("ullTotalPageFile", c_uint64),
        ("ullAvailPageFile", c_uint64),
        ("ullTotalVirtual", c_uint64),
        ("ullAvailVirtual", c_uint64),
        ("ullAvailExtendedVirtual", c_uint64),
    ]


def _get_system_info() -> dict:
    """Sample CPU and memory usage via native Windows APIs. Returns dict with keys
    cpu_pct, mem_used_pct, mem_total_gb, mem_avail_gb, or empty dict on failure."""
    if ctypes is None:
        return {}
    try:
        # Memory
        ms = _MEMORYSTATUSEX()
        ms.dwLength = sizeof(_MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(byref(ms))
        # CPU (two-sample delta)
        idle1, kernel1, user1 = _FILETIME(), _FILETIME(), _FILETIME()
        ctypes.windll.kernel32.GetSystemTimes(byref(idle1), byref(kernel1), byref(user1))
        time.sleep(0.3)
        idle2, kernel2, user2 = _FILETIME(), _FILETIME(), _FILETIME()
        ctypes.windll.kernel32.GetSystemTimes(byref(idle2), byref(kernel2), byref(user2))

        def _ft(v):
            return (v.dwHighDateTime << 32) | v.dwLowDateTime

        idle_d = _ft(idle2) - _ft(idle1)
        kernel_d = _ft(kernel2) - _ft(kernel1)
        user_d = _ft(user2) - _ft(user1)
        total_d = kernel_d + user_d
        cpu = round(100 * (total_d - idle_d) / total_d) if total_d > 0 else 0

        return {
            "cpu_pct": cpu,
            "mem_used_pct": ms.dwMemoryLoad,
            "mem_total_gb": round(ms.ullTotalPhys / (1024**3), 1),
            "mem_avail_gb": round(ms.ullAvailPhys / (1024**3), 1),
        }
    except Exception:
        return {}


def _strip_status_prefix(text: str) -> str:
    for p in STATUS_PREFIXES:
        if text.startswith(p):
            return text[len(p):].strip()
    return text


def _strip_check_prefix(text: str) -> str:
    for p in CHECK_PREFIXES:
        if text.startswith(p):
            return text[len(p):].strip()
    return text


def run_status(open_id: str) -> None:
    """Report PC and bot status."""
    lines = ["🖥 **PC 状态**"]

    info = _get_system_info()
    if info:
        lines.append(
            f"  CPU: {info['cpu_pct']}%  |  "
            f"内存: {info['mem_used_pct']}% "
            f"({info['mem_avail_gb']:.1f} / {info['mem_total_gb']:.1f} GB)"
        )
    else:
        lines.append("  (系统信息获取失败 — 非 Windows 平台)")

    uptime = time.time() - BOT_START_TIME
    h, m = divmod(int(uptime), 3600)
    m, s = divmod(m, 60)
    lines.append(f"  Bot 运行: {h}h {m}m {s}s  |  最大并发: {MAX_CONCURRENT} 槽位")

    lines.append(f"  {', '.join(EXEC_PREFIXES)}  |  {', '.join(AUTO_PREFIXES)}  |  {', '.join(FILE_PREFIXES)}")
    send_message(open_id, "\n".join(lines))


# ============================================================
# Directory Browser
# ============================================================
_DISPLAY_PREFIXES = {
    "pdf": "📕", "doc": "📘", "docx": "📘", "xls": "📗", "xlsx": "📗",
    "ppt": "📙", "pptx": "📙", "csv": "📊",
    "png": "🖼", "jpg": "🖼", "jpeg": "🖼", "gif": "🖼",
    "bmp": "🖼", "svg": "🖼", "webp": "🖼",
    "zip": "📦", "tar": "📦", "gz": "📦", "7z": "📦", "rar": "📦",
    "mp3": "🎵", "mp4": "🎬", "wav": "🎵",
    "py": "🐍", "js": "📜", "ts": "📜", "json": "📜", "md": "📜",
    "html": "🌐", "css": "🎨",
    "txt": "📝", "log": "📝",
    "exe": "⚙", "msi": "⚙", "bat": "⚙", "ps1": "⚙",
    "sh": "🐚",
}
_DIR_ICON = "📁"
_FILE_ICON = "📄"


def _list_dir(path: str) -> list[tuple[str, bool, int, float]]:
    """Return sorted (name, is_dir, size, mtime) for a directory."""
    items: list[tuple[str, bool, int, float]] = []
    try:
        with os.scandir(path) as entries:
            for e in entries:
                try:
                    st = e.stat()
                except OSError:
                    st = None
                items.append((
                    e.name,
                    e.is_dir(),
                    st.st_size if st else 0,
                    st.st_mtime if st else 0,
                ))
    except PermissionError:
        raise
    # dirs first, then alphabetical
    items.sort(key=lambda x: (not x[1], x[0].lower()))
    return items


def _fmt_size(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f}KB"
    return f"{size / (1024 * 1024):.1f}MB"


def _icon_for(name: str, is_dir: bool) -> str:
    if is_dir:
        return _DIR_ICON
    ext = os.path.splitext(name)[1].lower().lstrip(".")
    return _DISPLAY_PREFIXES.get(ext, _FILE_ICON)


_CHECK_PAGE = 20


def run_check(open_id: str, user_text: str) -> None:
    """Browse a directory and send listing to Feishu."""
    clean = _strip_check_prefix(user_text)

    if not clean:
        # Default: Desktop
        path = os.path.expanduser("~/Desktop")
    else:
        path = os.path.expandvars(os.path.expanduser(clean))
        if not os.path.isabs(path):
            # Match Chinese / English aliases to SEARCH_ROOTS
            _alias_map = {
                "桌面": "desktop", "desktop": "desktop",
                "下载": "downloads", "downloads": "downloads",
                "文档": "documents", "documents": "documents",
                "项目": "project", "project": "project", "bot": "project",
            }
            target = _alias_map.get(clean.lower(), clean.lower())
            for label, root in SEARCH_ROOTS:
                if target == label.lower():
                    path = root
                    break
            else:
                path = os.path.abspath(path)

    path = os.path.normpath(path)

    if not os.path.isdir(path):
        send_message(open_id, f"❌ 目录不存在：{path}")
        return

    try:
        all_items = _list_dir(path)
    except PermissionError:
        send_message(open_id, f"❌ 无权限访问：{path}")
        return

    if not all_items:
        send_message(open_id, f"📁 {path}\n   (空目录)")
        return

    page = all_items[:_CHECK_PAGE]
    remainder = len(all_items) - _CHECK_PAGE

    dir_count = sum(1 for _, d, _, _ in all_items if d)
    file_count = len(all_items) - dir_count

    lines = [f"📁 {path}"]
    lines.append(f"   {dir_count} 个文件夹 · {file_count} 个文件\n")

    for name, is_dir, size, _mtime in page:
        icon = _icon_for(name, is_dir)
        detail = _fmt_size(size) if not is_dir else ""
        line = f"  {icon} {name}"
        if detail:
            line += f"  ({detail})"
        lines.append(line)

    if remainder > 0:
        lines.append(f"\n… 还有 {remainder} 项。回复 more 展开下一页（60s 内有效）")
        expiry = time.time() + 60
        with _pending_lock:
            _pending_checks[open_id] = (expiry, path, all_items, _CHECK_PAGE)

    send_message(open_id, "\n".join(lines))


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

        # ── Check for pending file selection confirmation ──
        with _pending_lock:
            pending = _pending_selections.get(open_id)
            if pending is not None:
                expiry, candidates = pending
                if time.time() > expiry:
                    del _pending_selections[open_id]
                    send_message(open_id, "⏰ 文件选择已过期，请重新发送 !file <关键词>。")
                    return
                try:
                    idx = int(user_text.strip()) - 1
                    if 0 <= idx < len(candidates):
                        del _pending_selections[open_id]
                    else:
                        send_message(open_id,
                            f"❌ 序号超出范围（1-{len(candidates)}），请重新输入。")
                        return
                except ValueError:
                    # Not a number — user sent something else, leave pending intact
                    pass
                else:
                    path = candidates[idx]
                    size = os.path.getsize(path)
                    size_display = (
                        f"{size / 1024:.0f}KB" if size < 1024 * 1024
                        else f"{size / (1024 * 1024):.1f}MB"
                    )
                    send_message(open_id,
                        f"📤 正在上传：{os.path.basename(path)} ({size_display})…")
                    send_file_message(open_id, path)
                    return

            # ── Check for "more" pagination ──
            if user_text.lower().strip() in ("more", "m"):
                ck = _pending_checks.get(open_id)
                if ck is not None:
                    expiry, dir_path, all_items, offset = ck
                    if time.time() > expiry:
                        del _pending_checks[open_id]
                        send_message(open_id, "⏰ 已过期，请重新发送 !check <目录>。")
                        return

                    page = all_items[offset:offset + _CHECK_PAGE]
                    remainder = len(all_items) - offset - len(page)

                    for name, is_dir, size, _mtime in page:
                        icon = _icon_for(name, is_dir)
                        detail = _fmt_size(size) if not is_dir else ""
                        line = f"  {icon} {name}"
                        if detail:
                            line += f"  ({detail})"
                        send_message(open_id, line)

                    if remainder > 0:
                        new_offset = offset + _CHECK_PAGE
                        _pending_checks[open_id] = (time.time() + 60, dir_path, all_items, new_offset)
                        send_message(open_id,
                            f"… 还有 {remainder} 项。回复 more 展开下一页（60s 内有效）")
                    else:
                        del _pending_checks[open_id]
                        send_message(open_id, "✅ 已显示全部项目。")
                    return

        if user_text.startswith(FILE_PREFIXES):
            t = threading.Thread(
                target=run_file_transfer,
                args=(open_id, user_text),
                daemon=True,
            )
            t.start()
            return

        if user_text.startswith(AUTO_PREFIXES):
            t = threading.Thread(
                target=run_auto_task,
                args=(open_id, user_text),
                daemon=True,
                name=f"auto-{uuid.uuid4().hex[:6]}",
            )
            t.start()
            return

        if user_text.startswith(STATUS_PREFIXES):
            t = threading.Thread(
                target=run_status,
                args=(open_id,),
                daemon=True,
            )
            t.start()
            return

        if user_text.startswith(CHECK_PREFIXES):
            t = threading.Thread(
                target=run_check,
                args=(open_id, user_text),
                daemon=True,
            )
            t.start()
            return

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
    logger.info("  Feishu x Claude Code Bot  v1.1")
    logger.info(f"  Claude:  {CLAUDE_EXE}")
    logger.info(f"  Exec:    {', '.join(EXEC_PREFIXES)} -> claude -p --output-format json")
    logger.info(f"  File:    {', '.join(FILE_PREFIXES)} -> upload & send file to phone")
    logger.info(f"  Auto:    {', '.join(AUTO_PREFIXES)} -> multi-step task with progress")
    logger.info(f"  Status:  {', '.join(STATUS_PREFIXES)} -> PC & bot health report")
    logger.info(f"  Check:   {', '.join(CHECK_PREFIXES)} -> browse directory contents")
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
