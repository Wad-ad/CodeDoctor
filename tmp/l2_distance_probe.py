"""查看 L2 原始 top3 与距离(绕过 0.45 阈值),判断失败原因。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import kb

CASES = [
    ("TypeError", "'NoneType' object is not subscriptable", "typeerror-none-operations"),
    ("SyntaxError", "expected ':'", "syntaxerror-missing-colon"),
    ("SyntaxError", "invalid character '，' (U+FF0C)", "syntaxerror-cn-punctuation"),
    ("SyntaxError", "invalid syntax", "syntaxerror-generic"),
    ("IndexError", "list index out of range", "indexerror-out-of-range"),
    ("KeyError", "'name'", "keyerror-missing-key"),
    ("IndentationError", "expected an indented block", "indentation-expected-block"),
]

store = kb._get_vectorstore()
if store is None:
    print("向量库不可用:", kb._vector_error)
    raise SystemExit

for etype, msg, expect in CASES:
    print("=" * 92)
    print(f"[期望] {expect}   ←  {etype}: {msg}")
    for doc, dist in store.similarity_search_with_score(f"{etype}: {msg}", k=3):
        eid = (doc.metadata or {}).get("entry_id")
        flag = "✔" if eid == expect else " "
        print(f"   {flag} {eid:<38} dist={dist:.4f}")
