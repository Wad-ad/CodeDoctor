"""Step 3 验证:Critic 审查能力 —— 治标拦截率 + 正确修复误杀率。"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import llm

# (名称, 原始代码, 报错, 修复代码, 期望是否应被打回)
CASES = [
    (
        "治标·删掉报错行",
        'scores = [90, 85, 88, 91]\ntotal = sum(scores)\nprint("平均分是:", total / count)\n',
        {"type": "NameError", "message": "name 'count' is not defined", "line": 3,
         "snippet": 'print("平均分是:", total / count)'},
        'scores = [90, 85, 88, 91]\ntotal = sum(scores)\n',
        True,
    ),
    (
        "治标·try/except 吞掉",
        'scores = [90, 85, 88, 91]\ntotal = sum(scores)\nprint("平均分是:", total / count)\n',
        {"type": "NameError", "message": "name 'count' is not defined", "line": 3,
         "snippet": 'print("平均分是:", total / count)'},
        'scores = [90, 85, 88, 91]\ntotal = sum(scores)\ntry:\n'
        '    print("平均分是:", total / count)\nexcept Exception:\n    pass\n',
        True,
    ),
    (
        "治标·偏离意图(平均值变 0)",
        'scores = [90, 85, 88, 91]\ntotal = sum(scores)\nprint("平均分是:", total / count)\n',
        {"type": "NameError", "message": "name 'count' is not defined", "line": 3,
         "snippet": 'print("平均分是:", total / count)'},
        'scores = [90, 85, 88, 91]\ncount = len(scores)\ntotal = sum(scores)\n'
        'print("平均分是:", 0)\n',
        True,
    ),
    (
        "正确·补上 count 定义",
        'scores = [90, 85, 88, 91]\ntotal = sum(scores)\nprint("平均分是:", total / count)\n',
        {"type": "NameError", "message": "name 'count' is not defined", "line": 3,
         "snippet": 'print("平均分是:", total / count)'},
        'scores = [90, 85, 88, 91]\ncount = len(scores)\ntotal = sum(scores)\n'
        'print("平均分是:", total / count)\n',
        False,
    ),
    (
        "正确·直接用 len()",
        'scores = [90, 85, 88, 91]\ntotal = sum(scores)\nprint("平均分是:", total / count)\n',
        {"type": "NameError", "message": "name 'count' is not defined", "line": 3,
         "snippet": 'print("平均分是:", total / count)'},
        'scores = [90, 85, 88, 91]\ntotal = sum(scores)\n'
        'print("平均分是:", total / len(scores))\n',
        False,
    ),
    (
        "正确·空列表提前返回 0",
        'def avg(scores):\n    return sum(scores) / len(scores)\nprint(avg([]))\n',
        {"type": "ZeroDivisionError", "message": "division by zero", "line": 2,
         "snippet": "return sum(scores) / len(scores)"},
        'def avg(scores):\n    if not scores:\n        return 0\n'
        '    return sum(scores) / len(scores)\nprint(avg([]))\n',
        False,
    ),
    (
        "正确·越界加边界检查",
        'items = [1, 2, 3]\nprint(items[3])\n',
        {"type": "IndexError", "message": "list index out of range", "line": 2,
         "snippet": "print(items[3])"},
        'items = [1, 2, 3]\nif len(items) > 3:\n    print(items[3])\nelse:\n'
        '    print("没有第 4 个元素")\n',
        False,
    ),
    (
        "正确·input 转 int",
        'age = input("年龄:")\nprint(age + 1)\n',
        {"type": "TypeError", "message": 'can only concatenate str (not "int") to str', "line": 2,
         "snippet": "print(age + 1)"},
        'age = int(input("年龄:"))\nprint(age + 1)\n',
        False,
    ),
]

print("=" * 72)
print("Critic 审查能力测试")
print("=" * 72)

results = []
for name, code, err, fixed, should_revise in CASES:
    got = llm.review(code=code, error=err, fixed_code=fixed)
    if got is None:
        print(f"  [SKIP] {name} · 模型未返回(降级放行)")
        results.append((name, should_revise, None))
        continue
    verdict = got.get("verdict")
    blocked = verdict in ("revise", "reject")
    ok = blocked == should_revise
    issues = got.get("issues") or []
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print(f"         verdict={verdict}  blocking={got.get('blocking')}  "
          f"期望={'打回' if should_revise else '放行'}")
    if issues:
        print(f"         理由: {issues[0][:110]}")
    results.append((name, should_revise, blocked))

print()
print("=" * 72)
bad_cases = [r for r in results if r[1] is True]
good_cases = [r for r in results if r[1] is False]

caught = sum(1 for _, _, b in bad_cases if b is True)
false_kill = sum(1 for _, _, b in good_cases if b is True)
degraded = sum(1 for _, _, b in results if b is None)

print(f"治标拦截: {caught}/{len(bad_cases)}")
print(f"误杀: {false_kill}/{len(good_cases)}")
print(f"降级放行: {degraded}/{len(results)}")
print()
ok_all = caught == len(bad_cases) and false_kill <= 1
print("RESULT:", "PASS" if ok_all else "FAIL")
sys.exit(0 if ok_all else 1)
