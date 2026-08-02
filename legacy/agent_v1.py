"""
agent.py — Claude Code /loop 任务处理器

由 Claude Code 的 /loop 定时调用。检查 tasks/in/ 目录，
取出最早的一条待处理任务，打印供 Claude Code 执行。

用法（在 Claude Code 交互式会话中）：
  /loop 10s !python ~/Desktop/deepseek-feishu-bot/agent.py

Claude Code 看到输出后自然理解任务、执行并将结果写入
tasks/out/{task_id}.json，然后删除 tasks/in/ 中的原文件。
"""

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
IN_DIR = BASE_DIR / "tasks" / "in"
OUT_DIR = BASE_DIR / "tasks" / "out"

# 确保目录存在
IN_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)


def get_next_task() -> dict | None:
    """取出最早的一条待处理任务（按文件名排序）。"""
    files = sorted(IN_DIR.glob("*.json"))
    if not files:
        return None
    task_file = files[0]
    try:
        task = json.loads(task_file.read_text(encoding="utf-8"))
        task["_file"] = str(task_file)
        return task
    except (json.JSONDecodeError, OSError):
        # 损坏文件移到 ignored 目录
        bad_dir = IN_DIR / ".bad"
        bad_dir.mkdir(exist_ok=True)
        task_file.rename(bad_dir / task_file.name)
        return None


def main() -> None:
    task = get_next_task()
    if task is None:
        return  # 无任务，静默退出

    task_file = Path(task.pop("_file"))
    task_id = task.get("task_id", "")
    open_id = task.get("open_id", "")
    text = task.get("text", "")
    timestamp = task.get("timestamp", "")

    out_path = OUT_DIR / f"{task_id}.json"

    print(f"""
======================================================================
  NEW TASK — Claude Code please execute
======================================================================
  Task ID:   {task_id}
  User ID:   {open_id}
  Time:      {timestamp}
  Command:   {text}
======================================================================

Please execute the task above, then write the result to:
  {out_path}

Result JSON format:
  {{
    "task_id": "{task_id}",
    "open_id": "{open_id}",
    "result": "<execution output here>"
  }}

After writing the result, delete the task file:
  {task_file}
""")


if __name__ == "__main__":
    main()
