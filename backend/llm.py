"""LLM 层:LangChain 模型工厂 + 结构化输出 + 诊断调用。

- 本地 Ollama 走 ChatOllama(原生结构化输出);云端 OpenAI 兼容接口走 ChatOpenAI;
- 主路径 with_structured_output(Pydantic):schema 级校验,字段即前端契约;
- 失败自动降级为 JSON 模式手动解析;再失败由调用方退回知识库规则模式;
- prompt 注入知识库参考条目与历次修复尝试,支撑多轮自我修正。
"""

import json
import re
from typing import Literal

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
    risk_notes: str = Field(
        default="",
        description="你改动的范围与潜在副作用(写给审查员看);没有风险就写「无」",
    )


class Review(BaseModel):
    """审查结论:修复方案能不能进真实运行。只审「是不是假修复」,不评价风格。"""

    verdict: Literal["pass", "revise", "reject"] = Field(
        description="pass=进入真实运行;revise=发现假修复必须重写;reject=用户需求本身无法用改代码满足(极少用)"
    )
    issues: list[str] = Field(
        description="具体问题,每条写明「问题 + 第几行的证据 + 应该怎么改」。"
                    "verdict=revise 时至少一条;verdict=pass 时填空数组"
    )
    blocking: bool = Field(
        description="是否存在必须打回的严重问题;verdict=pass 时必须为 false"
    )


class Triage(BaseModel):
    """分诊结论:先定性(什么病),再定修法(怎么治)。策略是倾向,不是硬约束。"""

    error_category: Literal["syntax", "logic", "environment", "data", "unknown"] = Field(
        description="错误类别:syntax 语法/缩进;logic 逻辑与运行时;environment 环境依赖;data 数据问题;unknown 判断不了"
    )
    repair_strategy: Literal["minimal_fix", "logic_rewrite", "env_guide"] = Field(
        description="修复策略:minimal_fix 只改必要位置;logic_rewrite 重写相关逻辑;env_guide 给环境/依赖指引"
    )
    focus: str = Field(default="", description="重点关注的行或变量,一句话;没有就留空")
    confidence: float = Field(default=0.6, ge=0.0, le=1.0, description="判断把握程度,0-1")


TRIAGE_PROMPT = """你是 CodeDoctor 的「分诊器」。唯一任务:在动手修改代码之前,判断这是什么类型的错误、该用哪种修法。

【错误类别 error_category】
- syntax:语法 / 缩进 / 标点问题,代码根本没法解析(SyntaxError、IndentationError、TabError)
- logic:代码能跑但逻辑或运行时出错(NameError、TypeError、ZeroDivisionError、IndexError、KeyError)
- environment:环境、依赖、文件系统问题(ModuleNotFoundError、ImportError、FileNotFoundError)
- data:输入数据本身有问题(格式错、内容缺失、编码问题)
- unknown:信息不足,判断不了

【修复策略 repair_strategy】
- minimal_fix:局部性错误,改一两处即可,应最大程度保留用户原有代码
- logic_rewrite:算法或控制流本身有问题,需要重写相关逻辑
- env_guide:不该改代码,应给安装 / 配置 / 环境指引

【判断要点】
1. 以真实报错为准,不要凭空猜测;
2. 导入失败几乎总是 environment —— 除非用户明确不打算安装;
3. 如果代码的思路本身不成立(不是写错,而是想错了),选 logic_rewrite;
4. confidence 如实填写:信息充分给 0.8 以上,靠推测给 0.5 左右。

只输出 JSON,不要 markdown 围栏,不要任何多余文字。"""


# 分诊策略 → 修复提示(prompt 中的"倾向于怎么改",不是硬约束)
_STRATEGY_HINT = {
    "minimal_fix": (
        "这是局部性错误,只改必要的位置:最大程度保留用户原有的代码结构、变量名和注释,"
        "不要重构、不要引入新的抽象或额外的函数。"
    ),
    "logic_rewrite": (
        "问题可能出在算法或控制流本身:允许重写相关逻辑,但必须保留原有的输入输出语义、"
        "对外行为和变量命名习惯,不要改变程序最终想达成的结果。"
    ),
    "env_guide": (
        "这可能是环境或依赖问题:优先在 explain 里说明需要安装什么 / 配置什么;"
        "如果用户环境无法改变,才给出等价的标准库替代方案,并明确说明「这是替代方案,不是原方案」。"
    ),
}


REVIEW_PROMPT = """你是 CodeDoctor 的「审查员」。修复方案还没有运行,你的任务是在真跑之前判断它是不是"假修复"。

【先问自己三个问题】
1. 修复后的代码,还在做原来要做的事吗?原来会打印的结果,现在还会打印吗?
2. 报错是因为"问题被真正解决"而消失,还是因为"触发问题的代码被删掉 / 被吞掉"而消失?
3. 原来能产生的结果,修复后还能产生吗?如果输出变少了、消失了、或者数值不对了,就是假修复。

【只审这四件事】其他一概不管(不要评价代码风格、不要提优化建议、不要要求重构):
1. 治标:靠删除报错行、删掉整条语句、裸 except 吞异常、直接 return 假值等方式让报错消失,但问题其实还在。
   ——特别注意:如果**在原本能正常完成的输入下**,原本的 print / return / 计算不见了、程序不再产生原来的结果,
   这就是治标,即使它现在能跑通。
   ——反过来:原代码**本来就因此报错的异常输入**(除零时的 b=0、空列表、越界下标),加上 if 判断后在这条分支
   不输出或给提示,属于正常防护,**不算治标**。
   ——检查时必须看**完整的分支**:如果用了 if/else,且 else 分支给出了提示或合理处理,两条路径都有交代,
   就不算"输出消失"。不要只看到 if 条件就下结论。
2. 偏离意图:把用户原本想做的事改掉了(求平均变成返回 0、求和变成求个数、改写算法导致结果不同);
   ——注意区分:**原本就是异常输入**(空列表、越界、除零)被加上边界检查并给出提示 / 合理默认值,
   属于正常修复,不算偏离意图;只有**正常输入下结果被改错**(平均值变成 0)才算。
3. 新风险:引入了新问题(未定义变量、类型不一致、None 传播、空集合未处理、死循环);
4. 不完整:缺少 import、只给了片段、依赖了用户环境里没有的库。

【判定】
- pass:没发现上述问题,或只有不影响正确性的小瑕疵 → 进入真实运行
- revise:发现上述问题中至少一条,必须重写
- reject:用户的原始需求本身无法通过改代码满足(极少用)

【重要】
- 拿不准时选 pass。真跑会做最终裁决,**误杀的代价比漏审更大**。
- 但"输出消失 / 结果改变"是明确的假修复,不属于"拿不准",必须 revise。
- 每条 issue 必须写清楚:问题是什么 + 具体在第几行 + 应该怎么改。
- verdict=pass 时 blocking 必须为 false。

只输出 JSON,不要 markdown 围栏,不要任何多余文字。"""


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
                      attempts: list | None = None,
                      strategy: str = "", focus: str = "") -> str:
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
    # 分诊结论:告诉模型"倾向于怎么改",同时声明它只是建议
    if strategy and strategy in _STRATEGY_HINT:
        parts += ["", f"【修复策略建议(来自分诊)】{_STRATEGY_HINT[strategy]}"]
        if focus:
            parts.append(f"【重点关注】{focus}")
        parts.append("(以上是策略建议;若与实际报错冲突,以真实报错为准。)")
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
        parts += ["", "【历次尝试(都没通过,请针对性修正,不要重复同样的改法)】"]
        for a in attempts:
            if (a.get("gate") or "verify") == "critic":
                parts += [
                    f"--- 第 {a['round']} 版修复被审查打回:{a.get('error_summary') or '存在假修复问题'}",
                    "```python",
                    a.get("fixed_code", ""),
                    "```",
                    "请针对上述审查意见重写,不要沿用被指出的做法。",
                ]
            else:
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


def triage(code: str, error: dict | None, stderr: str = "", timed_out: bool = False,
           knowledge: str = "") -> dict | None:
    """LLM 分诊:L1 未命中时判断错误类别与修复策略。

    成功返回 {error_category, repair_strategy, focus, confidence, source="llm"};
    任何失败都返回 None(由调用方降级为 unknown),绝不抛异常阻断主链路。
    """
    if not config.API_KEY:
        return None

    parts = ["【用户代码】", "```python", code, "```", ""]
    if timed_out:
        parts += ["【运行结果】程序运行超时被强制终止,疑似死循环。", ""]
    else:
        parts += ["【真实报错(stderr 原文)】", (stderr or "").strip()[:2000] or "(无)", ""]
    if error:
        loc = f"第 {error['line']} 行" if error.get("line") else "位置未知"
        parts += [
            f"【报错定位】{loc},该行代码:{error.get('snippet', '')}",
            f"【错误类型】{error.get('type')}: {error.get('message')}",
        ]
    if knowledge:
        parts += ["", "【知识库参考(仅供判断,以真实报错为准)】", knowledge[:1500]]
    parts += ["", "请输出分诊 JSON。"]

    try:
        llm = build_chat_model()
        if config.is_ollama():
            structured = llm.with_structured_output(Triage)
        else:
            structured = llm.with_structured_output(Triage, method=config.STRUCTURED_METHOD)
        out = structured.invoke([("system", TRIAGE_PROMPT), ("human", "\n".join(parts))])
        data = out.model_dump() if isinstance(out, Triage) else Triage.model_validate(out).model_dump()
        data["source"] = "llm"
        return data
    except Exception:
        return None


def review(code: str, error: dict | None, fixed_code: str, knowledge: str = "",
           risk_notes: str = "") -> dict | None:
    """静态审查修复方案:抓真跑抓不到的假修复(治标 / 偏离意图 / 新风险 / 不完整)。

    成功返回 {verdict, issues, blocking};任何失败返回 None —— 调用方应当放行。
    fail-open 是有意设计:审查是增强,不该阻断主链路。
    """
    if not config.API_KEY or not (fixed_code or "").strip():
        return None

    parts = ["【用户的原始代码】", "```python", code, "```", "", "【真实报错】"]
    if error:
        parts.append(f"{error.get('type')}: {error.get('message')}")
        if error.get("line"):
            parts.append(f"报错位置:第 {error['line']} 行 → {error.get('snippet', '')}")
    else:
        parts.append("(程序运行超时,或没有解析到具体错误)")

    parts += ["", "【待审查的修复代码】", "```python", fixed_code, "```"]
    if (risk_notes or "").strip():
        parts += ["", "【修复者的改动自述】", risk_notes.strip()[:800]]
    if knowledge:
        parts += ["", "【知识库中这类错误的常规修法(供对照)】", knowledge[:1200]]
    parts += ["", "请输出审查 JSON。"]

    try:
        llm = build_chat_model()
        if config.is_ollama():
            structured = llm.with_structured_output(Review)
        else:
            structured = llm.with_structured_output(Review, method=config.STRUCTURED_METHOD)
        out = structured.invoke([("system", REVIEW_PROMPT), ("human", "\n".join(parts))])
        data = out.model_dump() if isinstance(out, Review) else Review.model_validate(out).model_dump()
    except Exception:
        return None

    # 规范化:verdict 是唯一权威,blocking 跟随它,避免模型给出自相矛盾的组合
    if data.get("verdict") == "pass":
        data["blocking"] = False
    elif data.get("verdict") in ("revise", "reject"):
        data["blocking"] = True
        # 没有给出任何具体理由的打回不可信:视为模型判断失效,放行交给真跑裁决
        if not [x for x in (data.get("issues") or []) if str(x).strip()]:
            return None
    return data


def diagnose(code: str, error: dict | None, stdout: str = "", stderr: str = "",
             timed_out: bool = False, knowledge: str = "", cases: str = "",
             attempts: list | None = None,
             strategy: str = "", focus: str = "") -> dict:
    """调用大模型产出结构化诊断。全部路径失败时抛异常,由调用方决定降级。"""
    msgs = [
        ("system", SYSTEM_PROMPT),
        ("human", build_user_prompt(code, error, stdout, stderr, timed_out,
                                    knowledge, cases, attempts, strategy, focus)),
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
    "字段顺序固定为 explain → reason → fix_points → error_line → fixed_code → risk_notes,格式:\n"
    '{"explain": "用一两句大白话说明程序发生了什么", '
    '"reason": "结合报错信息解释为什么会出错", '
    '"fix_points": ["第几行:把什么改成什么,为什么", "..."], '
    '"error_line": 报错行号或 null, '
    '"fixed_code": "修复后的完整代码,不要 markdown 围栏", '
    '"risk_notes": "这次改动的影响范围与潜在副作用;没有就写「无」"}\n'
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
                    attempts: list | None = None,
                    strategy: str = "", focus: str = ""):
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
                                    knowledge, cases, attempts, strategy, focus)),
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
                    timed_out=timed_out, knowledge=knowledge, cases=cases, attempts=attempts,
                    strategy=strategy, focus=focus)
    yield {"type": "final", "data": data}


# ---------------------------------------------------------------- 复用出口(chat.py 等模块使用)

build_stream_model = _build_stream_model
chunk_text = _chunk_text
extract_json = _extract_json
strip_code_fence = _strip_code_fence
