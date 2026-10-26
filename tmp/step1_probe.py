"""Step 1 验证:图结构 + 真实诊断链路(拆分 triage/fix 后行为应保持不变)。"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import graph

print("=" * 70)
print("[1] 图结构")
g = graph.GRAPH.get_graph()
print("  节点:", sorted(g.nodes.keys()))
print("  边:")
for e in g.edges:
    print(f"    {e.source} -> {e.target}")

print()
print("=" * 70)
print("[2] 真实诊断(除零错误 + 平均分逻辑)")

CODE = """def average(scores):
    total = 0
    for s in scores:
        total += s
    return total / count


scores = [90, 85, 88, 91]
print("平均分是:", average(scores))
"""

import runner
import traceback_parser

res = runner.run_python(CODE)
err = traceback_parser.parse_error(res["stderr"], CODE)
print(f"  真实报错: {err['type']}: {err['message']}")
print(f"  报错行: 第 {err['line']} 行 → {err['snippet']}")

diag = graph.run_diagnosis(CODE, err, res.get("stdout", ""), res.get("stderr", ""),
                            bool(res.get("timed_out")))

print()
print("[3] 交付结果关键字段")
print("  triage      =", json.dumps(diag.get("triage"), ensure_ascii=False))
print("  verified    =", diag.get("verified"))
print("  rounds      =", diag.get("verify_rounds"))
print("  verify_out  =", repr(diag.get("verify_output")))
print("  source      =", diag.get("source"))
print("  fix_points  =", json.dumps(diag.get("fix_points"), ensure_ascii=False)[:160])
print()
print("  修复代码:")
for line in (diag.get("fixed_code") or "").splitlines():
    print("    " + line)

print()
print("=" * 70)
print("[3.5] 分诊映射抽查(直接调节点,不调 LLM)")

map_cases = [
    ("SyntaxError", "invalid syntax", "syntax", "minimal_fix"),
    ("IndentationError", "unexpected indent", "syntax", "minimal_fix"),
    ("ZeroDivisionError", "division by zero", "logic", "minimal_fix"),
    ("NameError", "name 'x' is not defined", "logic", "minimal_fix"),
    ("ModuleNotFoundError", "No module named 'scipy'", "environment", "env_guide"),
    ("RecursionError", "maximum recursion depth exceeded", "logic", "logic_rewrite"),
    ("KeyError", "'name'", "logic", "minimal_fix"),
]
map_ok = True
for etype, msg, want_cat, want_strategy in map_cases:
    got = graph._triage_node({
        "error": {"type": etype, "message": msg, "line": 1, "snippet": "x = 1"},
        "timed_out": False,
    })["triage"]
    ok = (got["error_category"] == want_cat
          and got["repair_strategy"] == want_strategy
          and got["source"] == "l1")
    print(f"  [{'PASS' if ok else 'FAIL'}] {etype:20s} -> "
          f"{got['error_category']}/{got['repair_strategy']} ({got['source']})")
    map_ok = map_ok and ok

# 未收录类型 + 超时,应落到 unknown / __timeout__
edge = graph._triage_node({"error": {"type": "SomeUnknownError", "message": "?"}, "timed_out": False})["triage"]
timeout_t = graph._triage_node({"error": None, "timed_out": True})["triage"]
print(f"  [{'PASS' if edge['source'] != 'l1' else 'FAIL'}] 未收录类型 -> {edge['source']}(应为非 l1)")
print(f"  [{'PASS' if timeout_t['repair_strategy'] == 'logic_rewrite' else 'FAIL'}] 超时 -> {timeout_t['repair_strategy']}")
map_ok = map_ok and edge["source"] != "l1" and timeout_t["repair_strategy"] == "logic_rewrite"

print()
print("=" * 70)
print("[4] 断言")
triage = diag.get("triage") or {}
checks = {
    "图含 triage 节点": "triage" in g.nodes,
    "图含 fix 节点": "fix" in g.nodes,
    "图不含旧 diagnose 节点": "diagnose" not in g.nodes,
    "triage 已写入结果": bool(triage),
    "triage.source = l1": triage.get("source") == "l1",
    "name_error 判为 logic": triage.get("error_category") == "logic",
    "策略为 minimal_fix": triage.get("repair_strategy") == "minimal_fix",
    "命中知识条目": bool(triage.get("matched_id")),
    "验证通过": bool(diag.get("verified")),
    "有修复代码": bool((diag.get("fixed_code") or "").strip()),
    "分诊映射抽查全对": map_ok,
}

ok_all = True
for name, ok in checks.items():
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    ok_all = ok_all and ok

print()
print("RESULT:", "ALL PASS" if ok_all else "HAS FAILURE")
sys.exit(0 if ok_all else 1)
