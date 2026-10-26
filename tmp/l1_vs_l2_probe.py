"""L1 精确检索 vs L2 向量检索:同一批真实报错上的召回对比。"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import kb

CASES = [
    ("ZeroDivisionError", "division by zero"),
    ("TypeError", "unsupported operand type(s) for +: 'int' and 'str'"),
    ("TypeError", "'NoneType' object is not subscriptable"),
    ("NameError", "name 'count' is not defined"),
    ("SyntaxError", "expected ':'"),
    ("SyntaxError", "'(' was never closed"),
    ("SyntaxError", "invalid character '，' (U+FF0C)"),
    ("SyntaxError", "Missing parentheses in call to 'print'"),
    ("SyntaxError", "invalid syntax"),
    ("ModuleNotFoundError", "No module named 'scipy'"),
    ("IndexError", "list index out of range"),
    ("KeyError", "'name'"),
    ("AttributeError", "'NoneType' object has no attribute 'append'"),
    ("IndentationError", "unexpected indent"),
    ("IndentationError", "expected an indented block"),
    ("ValueError", "invalid literal for int() with base 10: 'abc'"),
    ("RecursionError", "maximum recursion depth exceeded"),
    ("UnicodeDecodeError", "'utf-8' codec can't decode byte 0xd6 in position 0"),
]

print("=" * 100)
print("L1 vs L2 召回对比(共 %d 条真实报错样本)" % len(CASES))
print("=" * 100)

t_l1 = t_l2 = 0.0
l1_hit = l2_hit_top1 = l2_type_ok = l2_any_in_top3 = 0
rows = []

for etype, msg in CASES:
    st = time.perf_counter()
    h1 = kb._l1_search(etype, msg)
    t_l1 += time.perf_counter() - st

    st = time.perf_counter()
    h2 = kb._l2_search(f"{etype}: {msg}", 3)
    t_l2 += time.perf_counter() - st

    e1 = h1[0]["id"] if h1 else "(未命中)"
    e2 = h2[0]["id"] if h2 else "(未命中)"
    d2 = "%.3f" % h2[0].get("distance", -1) if h2 else "-"
    expect = e1
    l1_hit += 1 if h1 else 0
    l2_hit_top1 += 1 if (h2 and h2[0]["id"] == expect) else 0
    l2_type_ok += 1 if (h2 and h2[0]["error_type"] == etype) else 0
    l2_any_in_top3 += 1 if (h2 and any(x["id"] == expect for x in h2)) else 0
    rows.append((etype, msg[:42], e1, e2, d2))

print(f"{'错误类型':<20} {'报错消息':<44} {'L1 top1':<34} {'L2 top1':<34} {'距离'}")
print("-" * 140)
for etype, msg, e1, e2, d2 in rows:
    print(f"{etype:<20} {msg:<44} {e1:<34} {e2:<34} {d2}")

n = len(CASES)
print("-" * 140)
print(f"L1 命中且确定:      {l1_hit}/{n}")
print(f"L2 top1 与 L1 一致: {l2_hit_top1}/{n}")
print(f"L2 top1 类型正确:   {l2_type_ok}/{n}")
print(f"L2 top3 内含正确项: {l2_any_in_top3}/{n}")
print(f"平均耗时: L1 {t_l1 / n * 1000:.2f} ms | L2 {t_l2 / n * 1000:.1f} ms")
