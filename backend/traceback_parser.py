"""解析 Python 真实的 traceback 文本,提取初学者最需要的信息。

产出:错误类型 / 错误消息 / 报错在用户代码中的行号 / 报错行的代码片段。
"""

import re

_FILE_LINE_RE = re.compile(r'File "([^"]+)", line (\d+)')
_EXC_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*)(?::\s?(.*))?$")

# Python 里的异常类名都以这些后缀结尾,用它们把「异常行」和普通输出区分开
_EXCEPTION_SUFFIXES = ("Error", "Exception", "Interrupt", "Exit", "Iteration", "Group")


def parse_error(stderr: str, code: str) -> dict | None:
    """从 stderr 中解析异常信息;没有可识别的异常时返回 None。"""
    if not stderr or not stderr.strip():
        return None

    lines = stderr.rstrip().splitlines()

    # 1) 从最后一行往上找「异常行」,例如 "ZeroDivisionError: division by zero"
    exc_type = None
    exc_msg = ""
    exc_index = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i]
        if not line or line[0] in " \t":
            continue
        m = _EXC_LINE_RE.match(line)
        if m and _looks_like_exception(m.group(1)):
            exc_type = m.group(1)
            exc_msg = (m.group(2) or "").strip()
            exc_index = i
            break

    if exc_type is None:
        return None

    # 2) 报错行号:从异常行往上找最后一个指向 main.py 的 File 行
    line_no = None
    for i in range(exc_index, -1, -1):
        m = _FILE_LINE_RE.search(lines[i])
        if m and m.group(1).replace("\\", "/").endswith("main.py"):
            line_no = int(m.group(2))
            break

    # 3) 报错行的代码内容直接从用户代码里取,比从 traceback 文本里抠更可靠
    snippet = ""
    code_lines = code.splitlines()
    if line_no and 1 <= line_no <= len(code_lines):
        snippet = code_lines[line_no - 1].strip()

    return {
        "type": exc_type,
        "message": exc_msg,
        "line": line_no,
        "snippet": snippet,
    }


def _looks_like_exception(name: str) -> bool:
    return name.endswith(_EXCEPTION_SUFFIXES)
