"""
bot_v2.py — 触发式版本（subprocess 直调 Claude Code）

与 bot.py（文件队列+轮询）的区别：
  /exec 消息 → 起子线程执行 claude -p → 完成后直接回复飞书
  无需 tasks/ 目录、无需 cron 轮询、零空转 token 消耗

用法（与 bot.py 相同）：
  python bot_v2.py
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
# 加载 .env 配置
# ============================================================
load_dotenv()
APP_ID = os.getenv("FEISHU_APP_ID", "").strip()
APP_SECRET = os.getenv("FEISHU_APP_SECRET", "").strip()
DS_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-v4-flash"
EXEC_PREFIXES = ("!exec", "!run", "！exec", "！run", "exec:", "run:")

# Claude CLI 路径 & 超时
CLAUDE_PATH = os.getenv("CLAUDE_PATH", os.path.expandvars(
    r"%USERPROFILE%\AppData\Roaming\npm\claude.cmd"
))
CLAUDE_TIMEOUT = int(os.getenv("CLAUDE_TIMEOUT", "300"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("feishu-bot-v2")


# ============================================================
# 调用 DeepSeek V4 Flash
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
# 调用 Claude Code CLI（子线程中执行）
# ============================================================
def call_claude(task_id: str, user_text: str) -> str:
    """
    执行 claude -p "<user_text>" 并返回输出。
    超时或异常时返回报错信息。
    """
    try:
        result = subprocess.run(
            [CLAUDE_PATH, "-p", user_text],
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT if CLAUDE_TIMEOUT > 0 else None,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode == 0:
            output = result.stdout.strip()
        else:
            output = (
                f"(exit={result.returncode})\n"
                f"{result.stdout.strip()}\n"
                f"{result.stderr.strip()}"
            ).strip()

        if not output:
            output = "(Claude 返回了空内容)"

        return output

    except subprocess.TimeoutExpired:
        logger.error(f"Claude timeout after {CLAUDE_TIMEOUT}s")
        return f"❌ Claude 执行超时（>{CLAUDE_TIMEOUT} 秒），任务可能仍在后台运行。"
    except FileNotFoundError:
        logger.error(f"claude CLI not found: {CLAUDE_PATH}")
        return f"❌ 未找到 claude，路径: {CLAUDE_PATH}\n请检查 .env 中 CLAUDE_PATH 配置。"
    except Exception as e:
        logger.error(f"Claude execution error: {e}")
        return f"❌ Claude 执行异常：{e}"


# ============================================================
# 通过飞书 REST API 回复用户消息
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
# 后台执行任务（子线程入口）
# ============================================================
def run_task(open_id: str, task_id: str, user_text: str) -> None:
    """在独立线程中调用 Claude，完成后直接回复飞书用户。"""
    start = time.time()
    logger.info(f"[{task_id}] 开始执行: {user_text[:80]}...")
    result = call_claude(task_id, user_text)
    elapsed = time.time() - start
    logger.info(f"[{task_id}] 完成 ({elapsed:.1f}s)")

    # 超长截断
    if len(result) > 15000:
        result = result[:15000] + "\n\n…（内容过长已截断）"

    send_message(open_id, result)


# ============================================================
# 事件回调 — 消息路由
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
            logger.warning(f"Non-JSON content ignored: {raw}")
            return
        user_text = (content_obj.get("text") or "").strip()
        if not user_text:
            return

        open_id = event.sender.sender_id.open_id
        logger.info(f"[{open_id}] >> {user_text}")

        # ──── 路由分发 ────
        if user_text.startswith(EXEC_PREFIXES):
            task_id = uuid.uuid4().hex[:8]
            # 先秒回确认
            send_message(open_id, f"⏳ Claude 正在处理…（{task_id}）")
            # 起子线程执行，不阻塞事件循环
            t = threading.Thread(
                target=run_task,
                args=(open_id, task_id, user_text),
                daemon=True,
                name=f"task-{task_id}",
            )
            t.start()
            return

        # ──── 普通消息 → DeepSeek ────
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
# 入口
# ============================================================
def main() -> None:
    missing = []
    if not APP_ID:
        missing.append("FEISHU_APP_ID")
    if not APP_SECRET:
        missing.append("FEISHU_APP_SECRET")
    if not DS_KEY:
        missing.append("DEEPSEEK_API_KEY")
    if missing:
        logger.error(f"缺少配置: {', '.join(missing)}，请检查 .env 文件。")
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
    logger.info("  Feishu × Claude Code Bot  V2 (trigger)")
    logger.info(f"  Claude CLI:  {CLAUDE_PATH}")
    logger.info(f"  命令前缀:   {', '.join(EXEC_PREFIXES)} → claude -p 子进程执行")
    logger.info(f"  超时限制:   {CLAUDE_TIMEOUT}s" if CLAUDE_TIMEOUT > 0 else "  无超时限制")
    logger.info(f"  普通消息:   DeepSeek ({DEEPSEEK_MODEL})")
    logger.info("=" * 50)
    ws.start()


if __name__ == "__main__":
    main()
