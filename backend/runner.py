"""CodeDoctor 代码执行引擎。

核心思路:绝不猜测报错——把用户代码写入独立临时目录,
用子进程真实执行一遍,捕获真实的 stdout / stderr。
超时自动终止,防止死循环卡死服务。

安全说明:
- 每个请求在独立的临时目录中执行,结束后目录自动清理;
- stdin 关闭(程序等输入时会直接报错而不是挂起);
- 输出长度截断,避免大量打印撑爆内存;
- 执行超时后强制终止子进程。
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import config

MAX_OUTPUT_CHARS = 20000


def _decode(data) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)


def run_python(code: str) -> dict:
    """真实执行一段 Python 代码,返回运行结果。"""
    with tempfile.TemporaryDirectory(prefix="codedoctor_") as tmp:
        script = Path(tmp) / "main.py"
        script.write_text(code, encoding="utf-8")

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"

        try:
            proc = subprocess.run(
                [sys.executable, "-X", "utf8", str(script)],
                capture_output=True,
                timeout=config.EXEC_TIMEOUT,
                cwd=tmp,
                env=env,
                stdin=subprocess.DEVNULL,
            )
            stdout = _decode(proc.stdout)
            stderr = _decode(proc.stderr)
            exit_code = proc.returncode
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            stdout = _decode(exc.stdout)
            stderr = _decode(exc.stderr)
            stderr += (
                f"\n[CodeDoctor] 程序运行超过 {config.EXEC_TIMEOUT} 秒仍没有结束,已被自动终止。\n"
                "最常见的原因:代码里有停不下来的死循环(比如 while True 没有退出条件)。"
            )
            exit_code = -1
            timed_out = True

        return {
            "ok": (exit_code == 0) and not timed_out,
            "stdout": stdout[:MAX_OUTPUT_CHARS],
            "stderr": stderr[:MAX_OUTPUT_CHARS],
            "exit_code": exit_code,
            "timed_out": timed_out,
        }
