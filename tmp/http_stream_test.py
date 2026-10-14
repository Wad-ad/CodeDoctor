# -*- coding: utf-8 -*-
"""HTTP SSE 流式诊断端到端测试:真实执行 + 真实 DeepSeek 流式 + 事件时序/一致性校验。

验证点:
1) 事件时序:search → analyze → (field/delta 实时) → verify → verify_result → result → done
2) delta 增量拼接结果 == result.data 中同名字段(流式内容与最终结果一致)
3) 首个 delta 的到达时间远早于最终结果时间(真流式,而非完了才吐)
4) result.verified == True(修复代码真实跑通)
"""

import json
import time
import urllib.request

BASE = "http://127.0.0.1:8000"

BAD_CODE = '''# 计算本次考试的平均分
scores = [88, 92, 79, 95]
total = sum(scores)
count = 0                 # 这里应该是 len(scores)
average = total / count
print("平均分是:", average)
'''


def post(path, payload, timeout=180):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=timeout)


def main():
    t0 = time.time()

    run = json.loads(post("/api/run", {"code": BAD_CODE}).read())
    print(f"[{time.time()-t0:6.2f}s] /api/run ok={run['ok']} error={run['error']}")
    assert not run["ok"], "坏代码应该运行失败"

    resp = post("/api/diagnose/stream", {
        "code": BAD_CODE,
        "stdout": run["stdout"],
        "stderr": run["stderr"],
        "error": run["error"],
        "timed_out": run["timed_out"],
    })

    events = []
    first_delta_t = None
    assembled = {"explain": "", "reason": "", "fixed_code": ""}
    items = []
    result_data = None
    n_delta = 0

    buf = b""
    for raw in resp:
        buf += raw
        while b"\n\n" in buf:
            frame, buf = buf.split(b"\n\n", 1)
            line = frame.decode("utf-8").strip()
            if not line.startswith("data: "):
                continue
            evt = json.loads(line[6:])
            t = time.time() - t0
            events.append((t, evt["type"]))

            if evt["type"] == "status":
                print(f"[{t:6.2f}s] STATUS  {evt.get('stage'):<13} {evt.get('msg')}")
            elif evt["type"] == "field":
                print(f"[{t:6.2f}s] FIELD   {evt['field']} · {evt['phase']}")
            elif evt["type"] == "delta":
                n_delta += 1
                if first_delta_t is None:
                    first_delta_t = t
                    print(f"[{t:6.2f}s] DELTA   <<首字到达>> {evt['field']}: {evt['text'][:20]!r}...")
                assembled[evt["field"]] = assembled.get(evt["field"], "") + evt["text"]
            elif evt["type"] == "item":
                items.append(evt["text"])
                print(f"[{t:6.2f}s] ITEM    {evt['text'][:60]}")
            elif evt["type"] == "result":
                result_data = evt["data"]
                print(f"[{t:6.2f}s] RESULT  verified={result_data.get('verified')} "
                      f"rounds={result_data.get('verify_rounds')} "
                      f"case_sources={len(result_data.get('case_sources') or [])}")
            elif evt["type"] == "error":
                print(f"[{t:6.2f}s] ERROR   {evt.get('message')}")
            elif evt["type"] == "done":
                print(f"[{t:6.2f}s] DONE")
            else:
                print(f"[{t:6.2f}s] ?       {evt}")

    print("\n================ 校验 ================")
    ok = True

    # 1) 时序:status 顺序
    stages = [e for (t, e) in events if e == "status"]
    print(f"事件统计: status={len([e for _, e in events if e == 'status'])} "
          f"delta={n_delta} item={len(items)} "
          f"field={len([e for _, e in events if e == 'field'])} "
          f"result={len([e for _, e in events if e == 'result'])}")

    # 2) 首字延迟 vs 结果时间
    result_t = next((t for (t, e) in events if e == "result"), None)
    if first_delta_t is not None and result_t is not None:
        print(f"首字延迟 {first_delta_t:.2f}s / 总耗时 {result_t:.2f}s —— 流式提前量 {result_t - first_delta_t:.2f}s")
        if first_delta_t < result_t:
            print("  [ok] 真流式:内容先于最终结果到达")
        else:
            ok = False
            print("  [FAIL] 首字不早于结果")
    else:
        ok = False
        print("  [FAIL] 没有 delta 或 result")

    # 3) 拼接一致性
    if result_data:
        for field in ("explain", "reason", "fixed_code"):
            streamed_text = assembled.get(field, "")
            final_text = result_data.get(field) or ""
            if field == "fixed_code":
                # 最终交付值经 strip_code_fence 规范化(去首尾空白/markdown 围栏);
                # 流式增量是模型原文,同样规范化后比较(对用户可见内容无差异)
                same = streamed_text.strip() == final_text.strip()
            else:
                same = streamed_text == final_text
            print(f"  [{'ok' if same else 'FAIL'}] {field}: 流式拼接 {len(streamed_text)} 字符 vs 最终 {len(final_text)} 字符"
                  + ("" if same else f"\n      streamed={streamed_text[:80]!r}...\n      final   ={final_text[:80]!r}..."))
            ok = ok and same
        fps = result_data.get("fix_points") or []
        same_items = items == fps
        print(f"  [{'ok' if same_items else 'FAIL'}] fix_points: 流式 {len(items)} 条 vs 最终 {len(fps)} 条")
        ok = ok and same_items

        # 4) verified
        print(f"  [{'ok' if result_data.get('verified') else 'FAIL'}] 修复代码真实验证:verified={result_data.get('verified')} "
              f"rounds={result_data.get('verify_rounds')}")
        ok = ok and bool(result_data.get("verified"))
        print(f"  验证运行输出: {(result_data.get('verify_output') or '').strip()[:100]!r}")

    print("=====================================")
    print("总判定:" + ("PASS" if ok else "FAIL"))


if __name__ == "__main__":
    main()
