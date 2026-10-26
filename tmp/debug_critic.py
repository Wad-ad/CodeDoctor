"""诊断回归失败排查:打印 triage / review / 每轮 attempts 与修复代码。"""

import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8000"
CODE = "a = 10\nb = 0\nprint(a / b)\n"


def post(path, payload):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return json.loads(urllib.request.urlopen(req, timeout=180).read())


run = post("/api/run", {"code": CODE})
print("真实报错:", run["error"]["type"], "| 第", run["error"]["line"], "行")
print()

resp = post("/api/diagnose", {
    "code": CODE, "stdout": run["stdout"], "stderr": run["stderr"],
    "error": run["error"], "timed_out": run["timed_out"],
})
d = resp["diagnosis"]

print("=" * 72)
print("triage :", json.dumps(d.get("triage"), ensure_ascii=False))
print("review :", json.dumps(d.get("review"), ensure_ascii=False))
print(f"verified={d.get('verified')}  rounds={d.get('verify_rounds')}")
print()
print("attempts:")
for a in d.get("attempts") or []:
    print(f"  第 {a.get('round')} 轮 | gate={a.get('gate')} | ok={a.get('ok')}")
    print(f"    {a.get('error')}")
print()
print("最终修复代码:")
for line in (d.get("fixed_code") or "").splitlines():
    print("    " + line)
