"""意图分类真实测试:LLM 必须正确区分 alternative / question。

重点关注需求中的指向性表述:
「为什么不那样改」「为什么不用那个方案」等必须识别为「提出替代修改方案」。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import chat  # noqa: E402

CASES = [
    ("为什么不那样改?", "alternative"),
    ("为什么不用那个方案?", "alternative"),
    ("为什么不用 try/except 包起来?", "alternative"),
    ("换成 while 循环试试", "alternative"),
    ("能不能用 len(scores) 直接算平均?", "alternative"),
    ("我觉得用字典推导式更简单", "alternative"),
    ("为什么把 count 改成 len(scores) 之后就不报错了?", "question"),
    ("为什么这样写能跑通?", "question"),
    ("这个错误常见吗?", "question"),
    ("ZeroDivisionError 是什么意思?", "question"),
    ("以后怎么避免这种错误?", "question"),
    ("谢谢你!", "question"),
]


def main():
    session = chat.ChatSession(session_id="intent-test")
    passed = 0
    for msg, expect in CASES:
        session.messages = [{"role": "user", "text": msg}]
        got, proposal = "?", ""
        try:
            result = chat.classify_intent(session, msg)
            got = result["intent"]
            proposal = result.get("proposal") or ""
        except Exception as exc:
            got = f"ERR:{type(exc).__name__}"
        ok = got == expect
        passed += ok
        mark = "[ok]" if ok else "[FAIL]"
        extra = f"  proposal={proposal}" if (ok and got == "alternative") else ""
        print(f"{mark} {msg}  -> {got} (期望 {expect}){extra}")
    print(f"\n{passed}/{len(CASES)} passed")
    sys.exit(0 if passed == len(CASES) else 1)


if __name__ == "__main__":
    main()
