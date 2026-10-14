"""LLM 层:LangChain 模型工厂 + 结构化输出 + 诊断调用。

- 本地 Ollama 走 ChatOllama(原生结构化输出);云端 OpenAI 兼容接口走 ChatOpenAI;
- 主路径 with_structured_output(Pydantic):schema 级校验,字段即前端契约;
- 失败自动降级为 JSON 模式手动解析;再失败由调用方退回知识库规则模式;
- prompt 注入知识库参考条目与历次修复尝试,支撑多轮自我修正。
"""

import json
import re

from pydantic import BaseModel, Field

import config
from stream_json import StreamFieldExtractor

SYSTEM_PROMPT = """你是 CodeDoctor,一名耐心、友善的编程助教,专门帮助编程初学者看懂代码报错。

用户会把一段刚运行失败的真实代码、真实报错信息,以及一份「参考知识」发给你。请遵守:
1. 只依据真实的报错信息分析,不要凭空猜测;参考知识与实际不符时,以真实报错为准。
2. 用大白话解释,像给刚学编程的朋友讲课;必须使用专业词时,紧跟一句通俗的解释。
3. 明确指出改哪一行、怎么改、为什么这样改。
4. fixed_code 必须是修改后的完整代码:最大程度保留用户原有的代码、变量名和注释,只做必要修改,不重构、不炫技。
5. 有多种改法时,选择对初学者最直观的那一种。
6. fix_points 至少给出一条:具体到第几行、把什么改成什么、为什么。
7. 你给出的 fixed_code 会被真实运行验证;如果验证失败,你会收到新的报错并被要求再修一次。
   请确保修复代码可以直接运行,不要依赖任何未安装的库。
"""


class Diagnosis(BaseModel):
    """结构化诊断结果:字段即前端契约,一处定义、全程使用。"""

    explain: str = Field(description="用一两句大白话说明程序发生了什么")
    reason: str = Field(description="结合报错信息,解释为什么会出现这个问题")
    fix_points: list[str] = Field(default_factory=list, description="修改点列表:每条写明第几行、改成什么、为什么")
    error_line: int | None = Field(default=None, description="报错所在行号(从 1 开始);没有就填 null")
    fixed_code: str = Field(description="修复后的完整代码;保留用户原有结构与注释,不要 markdown 围栏")


_model = None


def build_chat_model():
    global _model
    if _model is None:
        if config.is_ollama():
            from langchain_ollama import ChatOllama

            _model = ChatOllama(model=config.MODEL, base_url=config.ollama_base(), temperature=0.2)
        else:
            from langchain_openai import ChatOpenAI

            _model = ChatOpenAI(
                model=config.MODEL,
                api_key=config.API_KEY or "sk-none",
                base_url=config.BASE_URL,
                temperature=0.2,
            )
    return _model


def build_user_prompt(code: str, error: dict | None, stdout: str, stderr: str,
                      timed_out: bool, knowledge: str = "", cases: str = "",
                      attempts: list | None = None) -> str:
    parts = ["【用户代码】", "```python", code, "```", ""]
    if timed_out:
        parts += ["【运行结果】程序运行超时,被强制终止,没有正常结束。", ""]
    else:
        parts += ["【程序输出(stdout)】", stdout.strip() or "(无输出)", ""]
    parts += ["【真实报错(stderr 原文)】", stderr.strip() or "(无)", ""]
    if error:
        loc = f"第 {error['line']} 行" if error.get("line") else "位置未知"
        parts += [
            f"【报错定位】{loc},该行代码:{error.get('snippet', '')}",
            f"【错误类型】{error.get('type')}: {error.get('message')}",
        ]
    if knowledge:
        parts += [
            "",
            "【参考知识(初学者常见错误知识库,供参考;与实际情况不符时以真实报错为准)】",
            knowledge,
        ]
    if cases:
        parts += [
            "",
            "【参考案例(来自案例文档库,可能包含与当前问题相似的真实案例;仅供借鉴,以真实报错为准)】",
            cases,
        ]
    if attempts:
        parts += ["", "【历次修复尝试(以下修复代码运行后仍然报错,请针对性修正,不要重复同样的改法)】"]
        for a in attempts:
            parts += [
                f"--- 第 {a['round']} 轮修复版本运行后仍然报错:{a.get('error_summary') or '未知错误'}",
                "```python",
                a.get("fixed_code", ""),
                "```",
                f"该版本的真实报错原文:{a.get('stderr', '')}",
            ]
    parts += ["", "请输出结构化诊断:大白话解释 + 原因 + 至少一条修改点(fix_points) + 修复后的完整代码。"]
    return "\n".join(parts)


def _strip_code_fence(text: str) -> str:
    """去掉模型可能带上(或 JSON 字符串里残留)的 markdown 围栏。"""
    text = (text or "").strip()
    text = re.sub(r"^```[A-Za-z0-9_+.\-]*\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def _extract_json(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("AI 返回的内容里没有找到 JSON")
    return json.loads(cleaned[start:end + 1])


def _to_dict(result) -> dict:
    if isinstance(result, Diagnosis):
        data = result.model_dump()
    elif isinstance(result, dict):
        data = Diagnosis.model_validate(result).model_dump()
    else:
        raise ValueError(f"结构化输出结果类型异常:{type(result)}")
    data["fixed_code"] = _strip_code_fence(data.get("fixed_code") or "")
    return data


def diagnose(code: str, error: dict | None, stdout: str = "", stderr: str = "",
             timed_out: bool = False, knowledge: str = "", cases: str = "",
             attempts: list | None = None) -> dict:
    """调用大模型产出结构化诊断。全部路径失败时抛异常,由调用方决定降级。"""
    msgs = [
        ("system", SYSTEM_PROMPT),
        ("human", build_user_prompt(code, error, stdout, stderr, timed_out, knowledge, cases, attempts)),
    ]
    llm = build_chat_model()

    try:
        if config.is_ollama():
            structured = llm.with_structured_output(Diagnosis)
        else:
            structured = llm.with_structured_output(Diagnosis, method=config.STRUCTURED_METHOD)
        data = _to_dict(structured.invoke(msgs))
    except Exception as primary_exc:
        try:
            data = _json_mode_call(msgs)
        except Exception as fallback_exc:
            raise RuntimeError(
                f"结构化输出失败({primary_exc});JSON 模式兜底也失败({fallback_exc})"
            ) from fallback_exc

    data["source"] = "ai"
    data["model"] = config.MODEL
    return data


def _json_mode_call(msgs: list) -> dict:
    """兜底路径:提示词约束 JSON + 手动解析 + Pydantic 校验。"""
    system = SYSTEM_PROMPT + (
        "\n\n只输出一个合法 JSON 对象,字段:explain(字符串), reason(字符串), "
        "fix_points(字符串数组), error_line(整数或 null), fixed_code(字符串)。"
        "不要输出任何 JSON 之外的内容,不要 markdown 围栏。"
    )
    llm = build_chat_model()
    resp = llm.invoke([("system", system), ("human", msgs[1][1])])
    text = getattr(resp, "content", "") or ""
    if isinstance(text, list):
        text = "".join(str(x) for x in text)
    data = Diagnosis.model_validate(_extract_json(text)).model_dump()
    data["fixed_code"] = _strip_code_fence(data.get("fixed_code") or "")
    return data


# ---------------------------------------------------------------- 流式诊断

_JSON_HINT = (
    "\n\n输出要求:只输出一个合法 JSON 对象,不要 markdown 围栏、不要任何多余文字。"
    "字段顺序固定为 explain → reason → fix_points → error_line → fixed_code,格式:\n"
    '{"explain": "用一两句大白话说明程序发生了什么", '
    '"reason": "结合报错信息解释为什么会出错", '
    '"fix_points": ["第几行:把什么改成什么,为什么", "..."], '
    '"error_line": 报错行号或 null, '
    '"fixed_code": "修复后的完整代码,不要 markdown 围栏"}\n'
    "explain / reason / fix_points / fixed_code 会边生成边展示给用户,措辞要通俗友好。"
)


def _chunk_text(chunk) -> str:
    """从流式 chunk 取出文本增量(兼容 str 与 content blocks 两种形态)。"""
    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return ""


def _build_stream_model(json_mode: bool):
    """流式专用模型实例:优先带 JSON 强制提高输出可靠性,失败时再降档。"""
    if config.is_ollama():
        from langchain_ollama import ChatOllama

        kwargs = {"format": "json"} if json_mode else {}
        return ChatOllama(model=config.MODEL, base_url=config.ollama_base(),
                          temperature=0.2, **kwargs)

    from langchain_openai import ChatOpenAI

    model_kwargs = {"response_format": {"type": "json_object"}} if json_mode else {}
    return ChatOpenAI(
        model=config.MODEL,
        api_key=config.API_KEY or "sk-none",
        base_url=config.BASE_URL,
        temperature=0.2,
        model_kwargs=model_kwargs,
    )


def diagnose_stream(code: str, error: dict | None, stdout: str = "", stderr: str = "",
                    timed_out: bool = False, knowledge: str = "", cases: str = "",
                    attempts: list | None = None):
    """流式诊断(生成器):逐 token 消费模型输出,实时产出渲染事件。

    事件类型:
      {"type": "field", "field": ..., "phase": "start"/"end"}
      {"type": "delta", "field": ..., "text": ...}          实时文本增量
      {"type": "item",  "field": "fix_points", "text": ...} 数组条目完成
      {"type": "final", "data": {...完整诊断字典...}}        永远在最后

    降级链:流式+JSON 强制 → 流式纯提示词 → 非流式 diagnose()(原有三级降级)。
    无论哪条路径,最终都会产出「完整、经 Pydantic 校验」的 final 数据。
    """
    msgs = [
        ("system", SYSTEM_PROMPT + _JSON_HINT),
        ("human", build_user_prompt(code, error, stdout, stderr, timed_out,
                                    knowledge, cases, attempts)),
    ]

    streamed = False
    raw_parts: list = []
    extractor = StreamFieldExtractor()

    for json_mode in (True, False):
        raw_parts = []
        extractor = StreamFieldExtractor()
        try:
            for chunk in _build_stream_model(json_mode).stream(msgs):
                text = _chunk_text(chunk)
                if not text:
                    continue
                raw_parts.append(text)
                for evt in extractor.feed(text):
                    yield evt
            streamed = True
            break
        except Exception:
            continue   # 换下一档

    if streamed:
        try:
            data = Diagnosis.model_validate(_extract_json("".join(raw_parts))).model_dump()
            data["fixed_code"] = _strip_code_fence(data.get("fixed_code") or "")
            data["source"] = "ai"
            data["model"] = config.MODEL
            yield {"type": "final", "data": data}
            return
        except Exception:
            pass   # JSON 不完整 → 非流式兜底

    data = diagnose(code=code, error=error, stdout=stdout, stderr=stderr,
                    timed_out=timed_out, knowledge=knowledge, cases=cases, attempts=attempts)
    yield {"type": "final", "data": data}


# ---------------------------------------------------------------- 复用出口(chat.py 等模块使用)

build_stream_model = _build_stream_model
chunk_text = _chunk_text
extract_json = _extract_json
strip_code_fence = _strip_code_fence
