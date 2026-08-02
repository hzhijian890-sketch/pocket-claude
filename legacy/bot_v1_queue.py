import json
import logging
import os
import sys
import time
import threading
import uuid
from pathlib import Path

import requests
from dotenv import load_dotenv
from lark_oapi import Client as LarkClient
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
from lark_oapi.core.enum import LogLevel
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
from lark_oapi.ws import Client as WsClient

# ============================================================
# 路径 & 常量
# ============================================================
BASE_DIR = Path(__file__).resolve().parent
TASKS_IN_DIR = BASE_DIR / "tasks" / "in"
TASKS_OUT_DIR = BASE_DIR / "tasks" / "out"
TASKS_IN_DIR.mkdir(parents=True, exist_ok=True)
TASKS_OUT_DIR.mkdir(parents=True, exist_ok=True)

# ============================================================
# 加载 .env 配置
# ============================================================
load_dotenv()
APP_ID = os.getenv("FEISHU_APP_ID", "").strip()
APP_SECRET = os.getenv("FEISHU_APP_SECRET", "").strip()
DS_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-v4-flash"

# 命令前缀：以此开头的消息会路由给 Claude Code 执行
EXEC_PREFIXES = ("!exec", "!run", "！exec", "！run", "exec:", "run:")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("feishu-bot")


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
# 任务队列 — 写入 & 轮询
# ============================================================
def write_task(open_id: str, text: str) -> str:
    """将用户指令写入 tasks/in/{task_id}.json，返回 task_id。"""
    task_id = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
    task_file = TASKS_IN_DIR / f"{task_id}.json"
    payload = {
        "task_id": task_id,
        "open_id": open_id,
        "text": text,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    task_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"Task queued: {task_id}  [{open_id}]  {text[:80]}")
    return task_id


def poll_outgoing_results() -> None:
    """
    后台线程：轮询 tasks/out/ 目录，发现结果文件后通过飞书发给用户。
    处理完的文件移到同目录下的 .done/ 子目录，避免重复发送。
    """
    DONE_DIR = TASKS_OUT_DIR / ".done"
    DONE_DIR.mkdir(parents=True, exist_ok=True)

    while True:
        try:
            for f in sorted(TASKS_OUT_DIR.glob("*.json")):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    open_id = data.get("open_id", "")
                    result = data.get("result", "")
                    error = data.get("error")

                    if error:
                        text = f"❌ 任务执行出错：\n{error}"
                    else:
                        # 飞书单条消息有长度限制，超长截断并提示
                        if len(result) > 15000:
                            result = result[:15000] + "\n\n…（内容过长已截断）"
                        text = result

                    if open_id:
                        send_message(open_id, text)
                        logger.info(f"Result delivered for {f.stem}")
                except Exception as e:
                    logger.error(f"Failed to process result {f.name}: {e}")
                finally:
                    # 移到 .done 防止重复
                    try:
                        f.rename(DONE_DIR / f.name)
                    except Exception:
                        pass

        except Exception as e:
            logger.error(f"Watcher error: {e}")

        time.sleep(3)  # 每 3 秒检查一次


# ============================================================
# 事件回调 — 消息路由
# ============================================================
def handle_message(data) -> None:
    try:
        event = data.event
        message = event.message

        # 1. 只处理私聊
        if message.chat_type != "p2p":
            return

        # 2. 只处理文本消息
        if message.message_type != "text":
            return

        # 3. 解析 JSON content → 提取纯文本
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

        # 4. 路由分发
        if user_text.startswith(EXEC_PREFIXES):
            # ──── /exec /run → 写入任务队列，交给 Claude Code 执行 ────
            task_id = write_task(open_id, user_text)
            send_message(open_id, f"⏳ 任务已收到（{task_id[-8:]}），Claude 正在处理…")
        else:
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

    # 启动后台结果轮询线程（daemon 线程，主进程退出时自动结束）
    watcher = threading.Thread(target=poll_outgoing_results, daemon=True, name="out-watcher")
    watcher.start()
    logger.info("Result watcher thread started.")

    # 注册事件处理器 → WebSocket 长连接
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

    logger.info("🤖 飞书机器人启动（长连接模式），监听私聊消息...")
    logger.info(f"   命令前缀: {', '.join(EXEC_PREFIXES)} → 任务队列 → Claude Code 执行")
    logger.info(f"   其他消息 → DeepSeek ({DEEPSEEK_MODEL})")
    ws.start()


if __name__ == "__main__":
    main()
