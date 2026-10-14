"""跑通场景追问 HTTP 端到端测试。

场景:用户先遇到报错(有诊断),修好代码后运行通过 —— 此时追问功能应保留,
且带上「上一轮诊断」背景:
  1. 「为什么把 count 改成 len(scores) 之后就不报错了?」→ 自然回答(应参考上一轮背景)
  2. 「为什么不先检查一下 scores 是不是空的,再计算?」→ 替代方案(对已跑通的代码改一版)→ 真跑
  3. 「现在这版和最开始的版本相比,改了什么?」→ 多轮记忆 + 上下文
"""
import json
import sys
import time
import uuid

import requests

BASE = "http://127.0.0.1:8000"

BROKEN = '''# 计算本次考试的平均分
scores = [88, 92, 79, 95]
total = sum(scores)
count = 0                 # 这里应该是 len(scores)
average = total / count
print("平均分是:", average)
'''

FIXED = '''# 计算本次考试的平均分
scores = [88, 92, 79, 95]
total = sum(scores)
count = len(scores)       # 注意这一行
average = total / count
print("平均分是:", average)
'''


def sse(url, payload, on_event, timeout=240):
    t0 = time.time()
    with requests.post(url, json=payload, stream=True, timeout=timeout) as resp:
        resp.raise_for_status()
        buf = ""
        for raw in resp.iter_content(chunk_size=None):
            if not raw:
                continue
            buf += raw.decode("utf-8", errors="replace")
            while "\n\n" in buf:
                frame, buf = buf.split("\n\n", 1)
                line = frame.strip()
                if not line.startswith("data: "):
                    continue
                evt = json.loads(line[6:])
                on_event(evt, time.time() - t0)


def chat_turn(msg, sid, context, history, label):
    print(f"\n{'=' * 72}\n[{label}] 用户:{msg}\n{'-' * 72}")
    state = {"final": None, "text": ""}

    def on_evt(evt, dt):
        t = evt.get("type")
        if t == "status":
            print(f"  +{dt:5.1f}s  [状态] {evt.get('msg')}")
        elif t == "delta":
            state["text"] += evt["text"]
        elif t == "result":
            state["final"] = evt.get("data")
        elif t == "error":
            print(f"  +{dt:5.1f}s  [错误] {evt.get('message')}")

    sse(f"{BASE}/api/chat/stream", {
        "session_id": sid, "message": msg, "context": context, "history": history,
    }, on_evt)

    data = state["final"] or {}
    print(f"  [结果] kind={data.get('kind')}")
    text = state["text"].strip() or str(data.get("summary") or "")
    if text:
        print("  [回复] " + text.replace("\n", "\n         ")[:900])
    if data.get("kind") == "alt_success":
        print("  [替代代码]")
        for line in (data.get("code") or "").splitlines()[:14]:
            print("         " + line)
        print(f"  [真实运行输出] {data.get('stdout')!r}")
    if data.get("kind") == "alt_fail":
        for key in ("reasons", "limits", "issues"):
            for item in data.get(key) or []:
                print(f"  [{key}] {item}")
    return data, text


def main():
    # ---- 准备:真实报错 + 真实诊断(作为 prior),再跑通修复版
    print("STEP 0: 生成「上一轮诊断」背景…")
    run_bad = requests.post(f"{BASE}/api/run", json={"code": BROKEN}, timeout=30).json()
    assert not run_bad["ok"]
    print(f"  旧代码真实报错:{run_bad['error']['type']} @ 第 {run_bad['error']['line']} 行")
    prior_diag = requests.post(f"{BASE}/api/diagnose", json={
        "code": BROKEN, "stdout": run_bad["stdout"], "stderr": run_bad["stderr"],
        "error": run_bad["error"], "timed_out": run_bad["timed_out"],
    }, timeout=240).json()["diagnosis"]
    print(f"  旧代码诊断:verified={prior_diag.get('verified')}")

    run_ok = requests.post(f"{BASE}/api/run", json={"code": FIXED}, timeout=30).json()
    assert run_ok["ok"], "修复代码应该跑通"
    print(f"  修复代码运行通过,输出:{run_ok['stdout'].strip()!r}")

    # 跑通场景的会话上下文:diagnosis 为空,prior 携带上一轮
    context = {
        "code": FIXED,
        "stdout": run_ok["stdout"],
        "stderr": "",
        "error": None,
        "timed_out": False,
        "diagnosis": None,
        "prior": {"code": BROKEN, "stderr": run_bad["stderr"], "diagnosis": prior_diag},
    }
    sid = "ok-" + uuid.uuid4().hex[:8]
    history = []
    results = {}

    # 1. 追问「为什么这样改就好了」—— 依赖 prior 背景
    msg1 = "为什么把 count 改成 len(scores) 之后就不报错了?"
    d1, t1 = chat_turn(msg1, sid, context, history, "1 跑通后追问原因(依赖上一轮背景)")
    low1 = t1.lower()
    results["q1_kind"] = d1.get("kind") == "text"
    results["q1_len"] = len(t1) >= 15
    results["q1_mentions"] = ("len" in low1) or ("count" in low1)
    history += [{"role": "user", "text": msg1}, {"role": "assistant", "text": t1}]

    # 2. 对已跑通代码提替代方案
    msg2 = "为什么不先检查一下 scores 是不是空的,再计算平均分?"
    d2, t2 = chat_turn(msg2, sid, context, history, "2 跑通后替代方案(预期真跑)")
    results["alt2_kind"] = d2.get("kind") in ("alt_success", "alt_fail")
    results["alt2_has_if"] = "if" in (d2.get("code") or "")
    if d2.get("kind") == "alt_success":
        results["alt2_stdout"] = bool((d2.get("stdout") or "").strip())
    else:
        results["alt2_stdout"] = bool(d2.get("reasons"))
    history += [{"role": "user", "text": msg2}, {"role": "assistant", "text": t2}]

    # 3. 多轮记忆:对比新旧版本
    msg3 = "现在这版代码和最开始的版本相比,改了什么?"
    d3, t3 = chat_turn(msg3, sid, context, history, "3 多轮记忆(对比新旧版本)")
    low3 = t3.lower()
    results["q3_kind"] = d3.get("kind") == "text"
    results["q3_mentions"] = ("len" in low3) or ("count" in low3) or ("0" in low3)

    print(f"\n{'=' * 72}\n汇总:")
    for key, value in results.items():
        print(f"  {'PASS' if value else 'FAIL'}  {key}")
    all_ok = all(results.values())
    print("\n总体:" + ("全部通过 ✔" if all_ok else "存在失败项 ✖"))
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
