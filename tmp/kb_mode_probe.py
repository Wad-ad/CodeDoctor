"""按当前 CD_KB_MODE 统计 kb.search 的召回质量与耗时。"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import config
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

# 期望条目以 L1 的判定为基准(L1 是类型 + 正则的确定性匹配)
expect = {i: (kb._l1_search(t, m)[0]["id"] if kb._l1_search(t, m) else None) for i, (t, m) in enumerate(CASES)}

# 预热:初始化向量库(首次可能建索引),不计入耗时
kb.search({"type": "ZeroDivisionError", "message": "division by zero"}, top_k=3)

top1_ok = any_ok = empty = 0
elapsed = 0.0
misses = []
for i, (etype, msg) in enumerate(CASES):
    st = time.perf_counter()
    hits = kb.search({"type": etype, "message": msg}, top_k=3)
    elapsed += time.perf_counter() - st
    ids = [h["id"] for h in hits]
    if not ids:
        empty += 1
    if ids and ids[0] == expect[i]:
        top1_ok += 1
    if expect[i] in ids:
        any_ok += 1
    else:
        misses.append((etype, msg[:36], expect[i], ids))

n = len(CASES)
print(f"mode={config.KB_MODE}  max_distance={config.KB_MAX_DISTANCE}  vector={config.KB_VECTOR}")
print(f"  top1 正确 : {top1_ok}/{n}")
print(f"  top3 含正确: {any_ok}/{n}")
print(f"  完全无召回 : {empty}/{n}")
print(f"  平均耗时   : {elapsed / n * 1000:.1f} ms")
if misses:
    print("  --- 未召回正确的样本 ---")
    for etype, msg, exp, ids in misses:
        print(f"    {etype:<20} {msg:<38} 期望 {exp} -> 实际 {ids}")
