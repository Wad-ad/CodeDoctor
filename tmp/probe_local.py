"""探针:查看本地 Ollama 模型的完整诊断输出质量。"""
import json
import urllib.request

BASE = "http://127.0.0.1:8000"
CODE = 'scores = [90, 85, 92, 87]\ntotal = sum(scores)\ncount = 0\nprint("平均分是:", total / count)\n'


def post(path, payload):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read())


run = post("/api/run", {"code": CODE})
print("run.ok =", run.get("ok"), "| error =", (run.get("error") or {}).get("type"))
print()

diag = post("/api/diagnose", {
    "code": CODE,
    "stdout": run.get("stdout", ""),
    "stderr": run.get("stderr", ""),
    "error": run.get("error"),
    "timed_out": run.get("timed_out", False),
})
d = diag.get("diagnosis") or {}
print("verified =", d.get("verified"), "| rounds =", d.get("verify_rounds"), "| source =", d.get("source"))
print("verify_output =", repr(d.get("verify_output")))
print()
print("--- explain ---")
print(d.get("explain"))
print()
print("--- fix_points ---")
for p in d.get("fix_points") or []:
    print(" -", p)
print()
print("--- fixed_code ---")
print(d.get("fixed_code"))
