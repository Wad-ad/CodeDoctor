"""Step 3 图级验证:critic 三条分支(revise / pass / fail-open / reject)与 gate 记录。

用固定诊断替换 LLM 生成,只验证图逻辑与 State 流转,秒级完成。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import graph
import llm

CODE = "print(sum([1, 2, 3]) / 0)\n"
ERR = {"type": "ZeroDivisionError", "message": "division by zero", "line": 1,
       "snippet": "print(sum([1, 2, 3]) / 0)"}
STDERR = "ZeroDivisionError: division by zero"

FAKE_DIAG = {
    "explain": "测试用诊断",
    "reason": "测试",
    "fix_points": ["第 1 行:改成能跑的写法"],
    "error_line": 1,
    "fixed_code": "print('hello')\n",
    "risk_notes": "无",
    "source": "ai",
    "model": "fake",
}


def fake_stream(**kwargs):
    yield {"type": "final", "data": dict(FAKE_DIAG)}


llm.diagnose_stream = fake_stream

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}{(' · ' + detail) if detail else ''}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}{(' · ' + detail) if detail else ''}")


def run_with_review(review_result):
    llm.review = lambda **kw: review_result
    return graph.run_diagnosis(CODE, ERR, "", STDERR, False)


print("=" * 72)
print("[1] critic = revise(持续打回 → 第二次触发兜底放行)")
diag = run_with_review({"verdict": "revise", "issues": ["第 1 行:删掉了原有输出,是治标"],
                        "blocking": True})
attempts = diag.get("attempts") or []
review = diag.get("review") or {}
check("轨迹 = 打回一次 + 验证一次",
      len(attempts) == 2 and attempts[0].get("gate") == "critic"
      and attempts[1].get("gate") == "verify",
      f"实际 {[(a.get('gate'), a.get('round')) for a in attempts]}")
check("最终是第 2 版通过", diag.get("verify_rounds") == 2, f"实际 {diag.get('verify_rounds')}")
check("兜底放行后进入真跑", diag.get("verified") is True)
check("review.overridden = True", review.get("overridden") is True)
check("保留原始判定 revise", review.get("original_verdict") == "revise")
check("最终 verdict 已改为 pass", review.get("verdict") == "pass")
check("打回原因可读", "治标" in str(attempts[0].get("error") if attempts else ""))

print()
print("=" * 72)
print("[2] critic = pass(放行)")
diag = run_with_review({"verdict": "pass", "issues": [], "blocking": False})
attempts = diag.get("attempts") or []
check("验证通过", diag.get("verified") is True)
check("只有 1 条 attempt", len(attempts) == 1, f"实际 {len(attempts)}")
check("gate = verify", attempts and attempts[0].get("gate") == "verify")
check("review.verdict = pass", (diag.get("review") or {}).get("verdict") == "pass")
check("review.skipped = False", (diag.get("review") or {}).get("skipped") is False)

print()
print("=" * 72)
print("[3] critic = None(模型失败 → fail-open 放行)")
diag = run_with_review(None)
attempts = diag.get("attempts") or []
check("仍然完成验证", diag.get("verified") is True)
check("review.skipped = True", (diag.get("review") or {}).get("skipped") is True)
check("gate = verify", attempts and attempts[0].get("gate") == "verify")

print()
print("=" * 72)
print("[4] critic = reject(直接交付,不再重试)")
diag = run_with_review({"verdict": "reject", "issues": ["这个思路改代码无法实现"], "blocking": True})
attempts = diag.get("attempts") or []
check("未通过验证", diag.get("verified") is False)
check("只跑 1 轮就停", diag.get("verify_rounds") == 1, f"实际 {diag.get('verify_rounds')}")
check("attempts 1 条且为 critic", len(attempts) == 1 and attempts[0].get("gate") == "critic")
check("review.verdict = reject", (diag.get("review") or {}).get("verdict") == "reject")

print()
print("=" * 72)
print(f"汇总: PASS={PASS}  FAIL={FAIL}")
print("RESULT:", "ALL PASS" if FAIL == 0 else "HAS FAILURE")
sys.exit(0 if FAIL == 0 else 1)
