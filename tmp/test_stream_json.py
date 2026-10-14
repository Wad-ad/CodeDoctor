# -*- coding: utf-8 -*-
"""stream_json.StreamFieldExtractor 单元测试:分片/转义/乱序/脏输入 全覆盖。"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from stream_json import StreamFieldExtractor  # noqa: E402

PASS = 0
FAIL = 0
TARGETS = ("explain", "reason", "fix_points", "fixed_code")


def rebuild(events):
    """把事件流重建为字段字典,用于和原始数据对比。"""
    fields = {}
    for e in events:
        t = e["type"]
        if t == "field" and e["phase"] == "start":
            fields[e["field"]] = [] if e["field"] == "fix_points" else ""
        elif t == "delta":
            fields[e["field"]] += e["text"]
        elif t == "item":
            fields[e["field"]].append(e["text"])
    return fields


def check(name, got, expected):
    """比对:只关心 4 个目标字段;期望字典里其他键一律忽略。"""
    global PASS, FAIL
    ok = True
    for k, v in expected.items():
        if k not in TARGETS:
            continue
        if got.get(k) != v:
            ok = False
            print(f"  [FAIL] {name} · 字段 {k} 不一致")
            print(f"         got: {got.get(k)!r}")
            print(f"         exp: {v!r}")
    if ok:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
    return ok


def feed_all(text, sizes):
    ex = StreamFieldExtractor()
    events, pos = [], 0
    i = 0
    while pos < len(text):
        size = sizes[i % len(sizes)]
        events += ex.feed(text[pos:pos + size])
        pos += size
        i += 1
    return events


DATA = {
    "explain": "程序在计算平均分时除以了 0,Python 不允许这么算。",
    "reason": "count 被写死成了 0,总人数是 0 就没法求平均分,所以报了 ZeroDivisionError 错误。",
    "fix_points": ["第 4 行:把 count = 0 改成 count = len(scores)", "重新运行,程序会输出正确的平均分"],
    "error_line": 5,
    "fixed_code": "scores = [88, 92, 79, 95]\ntotal = sum(scores)\ncount = len(scores)\naverage = total / count\nprint(\"平均分是:\", average)\n",
}

print("=" * 60)
print("Case 1: 完整 JSON 一次喂入")
raw = json.dumps(DATA, ensure_ascii=False)
check("一次性",
      rebuild(feed_all(raw, [len(raw) + 10])),
      DATA)

print("Case 2: 分片喂入(2/3/5/7/11 字符循环,模拟 token 流)")
check("分片混合", rebuild(feed_all(raw, [2, 3, 5, 7, 11])), DATA)

print("Case 3: 逐字符喂入")
check("逐字符", rebuild(feed_all(raw, [1])), DATA)

print("Case 4: ensure_ascii=True(\\uXXXX 转义路径)")
raw_ascii = json.dumps(DATA)  # 默认 ensure_ascii=True
check("ASCII 转义分片", rebuild(feed_all(raw_ascii, [3, 6, 9])), DATA)

print("Case 5: 前后有废话(模型常见毛病)")
dirty = "好的,以下是诊断结果:\n" + raw + "\n希望对你有帮助!"
check("脏输入", rebuild(feed_all(dirty, [4, 13])), DATA)

print("Case 6: 字段乱序(fixed_code 在最前)")
shuffled = {"fixed_code": DATA["fixed_code"], "fix_points": DATA["fix_points"],
            "reason": DATA["reason"], "explain": DATA["explain"], "error_line": 5}
raw_s = json.dumps(shuffled, ensure_ascii=False)
check("乱序", rebuild(feed_all(raw_s, [5, 8])), DATA)

print("Case 7: 转义与 unicode(以标准 json.loads 的结果为基准)")
BS = chr(92)          # 反斜杠
DQ = chr(34)          # 双引号
NL = chr(10)          # 换行
TB = chr(9)           # 制表符
tricky = {
    "explain": "路径 C:" + BS + "Users" + BS + "me 下," + DQ + "引号" + DQ + "和" + NL + "换行都要对。",
    "reason": "emoji " + chr(0x1F600) + " 和裸代理对 " + chr(0xD83D) + chr(0xDE00) + " 都要与标准解析一致。",
    "fix_points": ["第 1 行:把 tab" + TB + "字符改成空格", "注意引号 " + DQ + " 的转义"],
    "fixed_code": "print(" + DQ + "he said " + BS + DQ + "hi" + BS + DQ + DQ + ")" + NL + "# 中文注释" + NL,
    "error_line": None,
}
raw_t = json.dumps(tricky, ensure_ascii=False)
check("转义·逐字符", rebuild(feed_all(raw_t, [1])),
      {k: v for k, v in json.loads(raw_t).items()})
raw_t2 = json.dumps(tricky)  # ensure_ascii=True:全量 \uXXXX 转义路径
check("转义·ASCII 分片", rebuild(feed_all(raw_t2, [2, 5])),
      {k: v for k, v in json.loads(raw_t2).items()})

# 手工原始 JSON(字面反斜杠转义序列,含代理对),期望同源 json.loads 生成
raw_manual = ("{" + DQ + "explain" + DQ + ":" + DQ + "emoji " + BS + "ud83d" + BS + "ude00 raw" + DQ
              + "," + DQ + "fix_points" + DQ + ":[]"
              + "," + DQ + "fixed_code" + DQ + ":" + DQ + "x" + DQ
              + "," + DQ + "reason" + DQ + ":" + DQ + "r" + DQ + "}")
check("手写代理对", rebuild(feed_all(raw_manual, [2, 4])),
      {k: v for k, v in json.loads(raw_manual).items()})

print("Case 8: 截断的坏 JSON(不崩溃,已发事件仍合法)")
half = raw[:len(raw) // 2]
events = feed_all(half, [3, 7])
got = rebuild(events)
ok8 = isinstance(got.get("explain"), str) and DATA["explain"].startswith(got.get("explain", ""))
check("截断不崩", {"explain_ok": ok8}, {"explain_ok": True})

print("Case 9: 非目标字段混合")
mixed = {
    "confidence": 0.93, "meta": {"nested": [1, 2, {"deep": "嵌套里也有引号\"和]括号"}], "ok": True},
    "explain": DATA["explain"], "reason": DATA["reason"],
    "fix_points": DATA["fix_points"], "error_line": 5, "fixed_code": DATA["fixed_code"],
}
raw_m = json.dumps(mixed, ensure_ascii=False)
check("嵌套跳过", rebuild(feed_all(raw_m, [1, 6, 17])), DATA)

print("=" * 60)
print(f"结果: PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
