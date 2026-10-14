"""知识库语料校验工具:改完 knowledge/errors.json 后运行,提前发现问题。

用法(在 backend 目录下运行):
    python check_kb.py                  # 校验默认语料 knowledge/errors.json
    python check_kb.py 路径/到/文件.json  # 校验指定文件

退出码:0 = 通过(可能带警告),1 = 发现错误(必须修复,否则诊断接口会报错)。
"""

import json
import re
import sys
from collections import Counter
from pathlib import Path

DEFAULT = Path(__file__).resolve().parent / "knowledge" / "errors.json"

REQUIRED = ("id", "error_type", "title")  # 缺失会直接影响诊断接口运行
LISTS = ("message_patterns", "scenarios", "fix_points", "tags")
TEXTS = ("explain", "example_bad", "example_good")


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    errors: list = []
    warnings: list = []

    print("=" * 56)
    print(f"CodeDoctor 知识库校验 | {path}")
    print("=" * 56)

    if not path.exists():
        print(f"  \u00d7 文件不存在:{path}")
        return 1

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        print("[结构]")
        print("  \u221a JSON 语法合法")
    except json.JSONDecodeError as exc:
        print(f"  \u00d7 JSON 语法错误,第 {exc.lineno} 行第 {exc.colno} 列:{exc.msg}")
        print("    常见原因:中文逗号/引号、对象末尾多逗号、引号没配对")
        return 1

    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        print("  \u00d7 顶层需要有 entries 字段,且为非空列表")
        return 1
    print(f"  \u221a entries:{len(entries)} 条")

    seen_ids: dict = {}
    type_count: Counter = Counter()
    fallback_count: Counter = Counter()

    for idx, e in enumerate(entries, start=1):
        if not isinstance(e, dict):
            errors.append(f"第 {idx} 条:不是 JSON 对象")
            continue

        eid = e.get("id")
        tag = f"[{eid}]" if isinstance(eid, str) and eid.strip() else f"第 {idx} 条"

        if not (isinstance(eid, str) and eid.strip()):
            errors.append(f"{tag}:缺少必填字段 id")
        else:
            if eid in seen_ids:
                errors.append(f"{tag}:id 与第 {seen_ids[eid]} 条重复")
            else:
                seen_ids[eid] = idx
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", eid):
                warnings.append(f"{tag}:id 建议用小写字母/数字/连字符,如 nameerror-undefined")

        for f in ("error_type", "title"):
            v = e.get(f)
            if not (isinstance(v, str) and v.strip()):
                errors.append(f"{tag}:缺少必填字段 {f}")

        etype = e.get("error_type")
        if isinstance(etype, str) and etype.strip():
            type_count[etype] += 1
            if not (etype == "__timeout__" or re.fullmatch(r"[A-Z][A-Za-z0-9_]*", etype)):
                warnings.append(
                    f"{tag}:error_type「{etype}」不像 Python 异常名"
                    "(应以大写字母开头;超时场景用 __timeout__)"
                )

        for f in LISTS:
            v = e.get(f)
            if v is None:
                warnings.append(f"{tag}:缺少列表字段 {f}")
            elif not isinstance(v, list) or any(not isinstance(x, str) for x in v):
                errors.append(f"{tag}:字段 {f} 必须是字符串列表")

        pats = e.get("message_patterns")
        if isinstance(pats, list):
            if not pats and isinstance(etype, str) and etype.strip():
                fallback_count[etype] += 1
            for p in pats:
                if not isinstance(p, str):
                    continue
                try:
                    re.compile(p)
                except re.error as exc:
                    errors.append(f"{tag}:message_patterns 里的正则不合法「{p}」({exc})")

        for f in TEXTS:
            v = e.get(f)
            if v is None:
                warnings.append(f"{tag}:缺少文本字段 {f}")
            elif not isinstance(v, str):
                errors.append(f"{tag}:字段 {f} 必须是字符串")
            elif f == "explain" and not v.strip():
                warnings.append(f"{tag}:explain 为空,会拉低诊断质量")

    for etype, n in fallback_count.items():
        if n > 1:
            warnings.append(
                f"error_type {etype} 有 {n} 条兜底条目(message_patterns 为空),建议只保留 1 条"
            )

    print()
    print("[汇总]")
    print(f"  条目:{len(entries)} | 错误类型:{len(type_count)} | 警告:{len(warnings)} | 错误:{len(errors)}")
    if errors:
        print()
        print(f"  \u00d7 {len(errors)} 个错误(必须修复,否则诊断接口会报错):")
        for m in errors:
            print(f"     - {m}")
    if warnings:
        print()
        print(f"  ! {len(warnings)} 个警告(不影响运行,建议改进):")
        for m in warnings:
            print(f"     - {m}")
    if not errors and not warnings:
        print("  \u221a 全部通过,没有发现问题")
    if not errors:
        print()
        print("  \u221a 可以重启后端了:新语料在重启后生效,向量索引会自动重建")
    return 1 if errors else 0


if __name__ == "__main__":
    # 保持系统默认输出编码(中文 Windows 为 GBK,可正常承载中文与 √× 符号);
    # errors="replace" 兜底,确保任何环境下都不因编码问题中断。
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    sys.exit(main())
