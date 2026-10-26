"""在指定 CD_KB_MODE 下跑一次真实诊断,看分诊来源、命中知识与验证结果。"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import config
import graph
import runner
import traceback_parser

CODE = """scores = [88, 92, 79]
count = 0
average = sum(scores) / count
print("平均分是:", average)
"""

result = runner.run_python(CODE)
error = traceback_parser.parse_error(result["stderr"], CODE)
print(f"模式: {config.KB_MODE} | 阈值: {config.KB_MAX_DISTANCE}")
print(f"真实报错: {error['type']}: {error['message']} (第 {error['line']} 行)")

start = time.perf_counter()
diag = graph.run_diagnosis(CODE, error, result["stdout"], result["stderr"], result["timed_out"])
cost = time.perf_counter() - start

triage = diag.get("triage") or {}
print(f"分诊: source={triage.get('source')} | {triage.get('error_category')} / {triage.get('repair_strategy')}")
print(f"命中知识: {diag.get('knowledge_ids')}")
print(f"验证: verified={diag.get('verified')} rounds={diag.get('verify_rounds')}")
print(f"验证输出: {(diag.get('verify_output') or '(空)').strip()[:80]}")
print(f"耗时: {cost:.1f}s")
print("修复代码:")
for line in (diag.get("fixed_code") or "").splitlines()[:8]:
    print("   ", line)
