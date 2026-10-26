"""Step 2 验证:L1 全类型映射 + 策略注入 prompt + LLM 分诊兜底。"""

import json
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

import graph
import kb
import llm

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}{(' · ' + detail) if detail else ''}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}{(' · ' + detail) if detail else ''}")


print("=" * 72)
print("[1] L1 全类型映射(errors.json 的每一类都要能被分诊识别)")

data = json.loads((BACKEND / "knowledge" / "errors.json").read_text(encoding="utf-8"))
sample = {}
for e in data["entries"]:
    et = e["error_type"]
    if et in sample:
        continue
    for p in e.get("message_patterns") or []:
        # pattern 自身能匹配自己,才拿它当样本消息
        if re.search(p, p, re.IGNORECASE):
            sample[et] = p
            break

print(f"  可构造样本的类型: {len(sample)} 类")
for et in sorted(sample):
    t = graph._triage_node({
        "error": {"type": et, "message": sample[et], "line": 3, "snippet": "x = 1"},
        "timed_out": False,
    })["triage"]
    want = graph._L1_TRIAGE_MAP.get(et)
    ok = (t["source"] == "l1"
          and want is not None
          and t["error_category"] == want[0]
          and t["repair_strategy"] == want[1])
    print(f"    {et:20s} {t['error_category']:12s}/{t['repair_strategy']:14s} "
          f"{'OK' if ok else 'MISMATCH'}")
    check(f"L1 映射 · {et}", ok)

# 超时是特殊类型,单独验证
t_timeout = graph._triage_node({"error": None, "timed_out": True})["triage"]
check("L1 映射 · __timeout__", t_timeout["error_category"] == "logic"
      and t_timeout["repair_strategy"] == "logic_rewrite"
      and t_timeout["source"] == "l1")

print()
print("=" * 72)
print("[2] 策略注入 build_user_prompt")

base = dict(code="print(1/0)", error={"type": "ZeroDivisionError", "message": "division by zero",
                                      "line": 1, "snippet": "print(1/0)"},
            stdout="", stderr="ZeroDivisionError: division by zero", timed_out=False)

p_default = llm.build_user_prompt(**base)
check("不传策略时不出现策略段", "【修复策略建议" not in p_default)

for strategy, keyword in (("minimal_fix", "局部性错误"),
                          ("logic_rewrite", "重写相关逻辑"),
                          ("env_guide", "环境或依赖问题")):
    p = llm.build_user_prompt(**base, strategy=strategy, focus="第 1 行的除法")
    check(f"策略 {strategy} 已注入", keyword in p and "【修复策略建议" in p)
    check(f"策略 {strategy} 带 focus", "第 1 行的除法" in p)

p_unknown = llm.build_user_prompt(**base, strategy="not_a_strategy")
check("非法策略值被忽略", "【修复策略建议" not in p_unknown)

print()
print("=" * 72)
print("[3] LLM 分诊兜底(未收录类型 → 真实调用模型)")

PERM_CODE = """f = open("/root/secret.txt")
print(f.read())
"""
got = llm.triage(
    code=PERM_CODE,
    error={"type": "PermissionError", "message": "[Errno 13] Permission denied: '/root/secret.txt'",
           "line": 1, "snippet": 'f = open("/root/secret.txt")'},
    stderr="PermissionError: [Errno 13] Permission denied: '/root/secret.txt'",
    timed_out=False,
)
print("  模型返回:", json.dumps(got, ensure_ascii=False) if got else "None(降级)")
check("LLM 分诊有返回", bool(got))
if got:
    check("字段完整", all(k in got for k in
                          ("error_category", "repair_strategy", "focus", "confidence", "source")))
    check("source = llm", got.get("source") == "llm")
    check("类别合法", got.get("error_category") in
          ("syntax", "logic", "environment", "data", "unknown"))
    check("策略合法", got.get("repair_strategy") in
          ("minimal_fix", "logic_rewrite", "env_guide"))
    check("PermissionError 判为 environment",
          got.get("error_category") == "environment",
          f"实际 {got.get('error_category')}")

print()
print("=" * 72)
print(f"汇总: PASS={PASS}  FAIL={FAIL}")
print("RESULT:", "ALL PASS" if FAIL == 0 else "HAS FAILURE")
sys.exit(0 if FAIL == 0 else 1)
