"""多轮追问 HTTP 端到端测试(真实 SSE + 真实执行)。

流程:
  0. /api/run 跑坏代码 → /api/diagnose 拿真实诊断(生成会话上下文)
  1. 普通追问(流式文本,上下文相关)
  2. 替代方案·真实跑通("为什么不直接用 sum(scores)/len(scores)?")
  3. 替代方案·走不通("能不能用 scipy 算?"→ 真实 ModuleNotFoundError)
  4. 多轮记忆(问刚才试过哪两种方案 —— 只能靠服务端会话历史回答)
"""
import json
import sys
import time
import uuid

import requests

BASE = "http://127.0.0.1:8000"

CODE = '''# 计算本次考试的平均分
scores = [88, 92, 79, 95]
total = sum(scores)
count = 0                 # 这里应该是 len(scores)
average = total / count
print("平均分是:", average)
'''


def sse(url, payload, on_event, timeout=240):
    t0 = time.time()
    first_delta = None
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
                dt = time.time() - t0
                if evt.get("type") == "delta" and first_delta is None:
                    first_delta = dt
                on_event(evt, dt)
    return first_delta


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

    fd = sse(f"{BASE}/api/chat/stream", {
        "session_id": sid, "message": msg, "context": context, "history": history,
    }, on_evt)

    data = state["final"] or {}
    print(f"  (首字 {fd:.1f}s 出现)" if fd else "  (无流式文本)")
    print(f"  [结果] kind={data.get('kind')}")
    text = state["text"].strip() or str(data.get("summary") or "")
    if text:
        print("  [回复] " + text.replace("\n", "\n         "))
    if data.get("kind") == "alt_success":
        print("  [替代代码]")
        for line in (data.get("code") or "").splitlines()[:14]:
            print("         " + line)
        print(f"  [真实运行输出] {data.get('stdout')!r}")
    if data.get("kind") == "alt_fail":
        print(f"  [infeasible] {data.get('infeasible')}")
        for key in ("reasons", "limits", "issues"):
            for item in data.get(key) or []:
                print(f"  [{key}] {item}")
        print(f"  [suggestion] {data.get('suggestion')}")
    return data, text


def main():
    print("STEP 0: 真实运行 + 诊断(生成上下文)…")
    run = requests.post(f"{BASE}/api/run", json={"code": CODE}, timeout=30).json()
    assert not run["ok"], "测试代码应该报错"
    err = run["error"]
    print(f"  真实报错:{err['type']}: {err['message']} @ 第 {err['line']} 行")

    diag = requests.post(f"{BASE}/api/diagnose", json={
        "code": CODE, "stdout": run["stdout"], "stderr": run["stderr"],
        "error": run["error"], "timed_out": run["timed_out"],
    }, timeout=240).json()["diagnosis"]
    print(f"  诊断完成:verified={diag.get('verified')} rounds={diag.get('verify_rounds')} "
          f"fixed_code 含 len(scores): {'len(scores)' in (diag.get('fixed_code') or '')}")

    context = {"code": CODE, "stdout": run["stdout"], "stderr": run["stderr"],
               "error": run["error"], "timed_out": run["timed_out"], "diagnosis": diag}
    sid = "e2e-" + uuid.uuid4().hex[:8]
    history = []
    results = {}

    # 1. 普通追问
    msg1 = "这个错误常见吗?以后怎么避免?"
    d1, t1 = chat_turn(msg1, sid, context, history, "1 普通追问(预期自然回答)")
    results["q_kind_text"] = d1.get("kind") == "text"
    results["q_text_len"] = len(t1) >= 15
    history += [{"role": "user", "text": msg1}, {"role": "assistant", "text": t1}]

    # 2. 替代方案 - 真实跑通
    msg2 = "为什么不直接用 average = sum(scores) / len(scores)?"
    d2, t2 = chat_turn(msg2, sid, context, history, "2 替代方案(预期真实跑通)")
    results["alt_kind_ok"] = d2.get("kind") == "alt_success"
    results["alt_code_len"] = "len(scores)" in (d2.get("code") or "")
    results["alt_stdout"] = bool((d2.get("stdout") or "").strip())
    history += [{"role": "user", "text": msg2}, {"role": "assistant", "text": t2}]

    # 3. 替代方案 - 走不通(scipy 未安装 → 真实 ModuleNotFoundError)
    msg3 = "能不能用 scipy 的 mean 函数来算平均分?"
    d3, t3 = chat_turn(msg3, sid, context, history, "3 替代方案(预期走不通+结构化解释)")
    results["fail_kind"] = d3.get("kind") == "alt_fail"
    results["fail_summary"] = len(str(d3.get("summary") or "")) >= 8
    results["fail_reasons"] = bool(d3.get("reasons"))
    blob = json.dumps(d3, ensure_ascii=False).lower()
    results["fail_evidence_scipy"] = ("scipy" in blob) or ("modulenotfound" in blob)
    history += [{"role": "user", "text": msg3}, {"role": "assistant", "text": t3}]

    # 4. 多轮记忆:只有服务端会话历史完整,才能答出两种方案
    msg4 = "我们刚才一共试过哪两种方案?各自的结果如何?"
    d4, t4 = chat_turn(msg4, sid, context, history, "4 多轮记忆(依赖服务端会话)")
    low = t4.lower()
    results["memory_kind"] = d4.get("kind") == "text"
    results["memory_mentions_len"] = ("len" in low) or ("sum" in low)
    results["memory_mentions_scipy"] = "scipy" in low

    print(f"\n{'=' * 72}\n汇总:")
    for key, value in results.items():
        print(f"  {'PASS' if value else 'FAIL'}  {key}")
    all_ok = all(results.values())
    print("\n总体:" + ("全部通过 ✔" if all_ok else "存在失败项 ✖"))
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
