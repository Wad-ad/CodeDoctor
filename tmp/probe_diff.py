"""探针:流式 fixed_code 拼接 vs 最终 fixed_code 的逐字符差异定位。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import llm  # noqa: E402

CODE = """a = 10
b = 0
print(a / b)
"""

streamed = ""
final = None
for evt in llm.diagnose_stream(
    code=CODE,
    error={"type": "ZeroDivisionError", "message": "division by zero", "line": 3, "snippet": "print(a / b)"},
    stdout="", stderr="Traceback (most recent call last):\n  File \"main.py\", line 3, in <module>\n    print(a / b)\nZeroDivisionError: division by zero\n",
):
    if evt.get("type") == "delta" and evt.get("field") == "fixed_code":
        streamed += evt["text"]
    if evt.get("type") == "final":
        final = evt["data"]["fixed_code"]

print("streamed len:", len(streamed))
print("final    len:", len(final))
print("streamed 末尾:", repr(streamed[-40:]))
print("final    末尾:", repr(final[-40:]))
print("streamed 开头:", repr(streamed[:40]))
print("final    开头:", repr(final[:40]))
print("strip 后相等:", streamed.strip() == final.strip())
