# -*- coding: utf-8 -*-
"""非流式 /api/diagnose 回归冒烟:确认原有链路零回归。"""
import json
import urllib.request

BASE = "http://127.0.0.1:8000"
code = "a = 10\nb = 0\nprint(a / b)\n"


def post(path, payload):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return json.loads(urllib.request.urlopen(req, timeout=120).read())


run = post("/api/run", {"code": code})
print("run:", run["ok"], run["error"]["type"], "line", run["error"]["line"])

resp = post("/api/diagnose", {
    "code": code, "stdout": run["stdout"], "stderr": run["stderr"],
    "error": run["error"], "timed_out": run["timed_out"],
})
d = resp["diagnosis"]
print("diagnose: source=%s verified=%s rounds=%s" % (d.get("source"), d.get("verified"), d.get("verify_rounds")))
print("explain:", (d.get("explain") or "")[:50])
print("verify_output:", repr((d.get("verify_output") or "").strip()[:40]))
assert d.get("verified") is True, "验证未通过"
print("REGRESSION PASS")
