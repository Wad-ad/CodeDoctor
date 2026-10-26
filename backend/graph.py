"""LangGraph 诊断-修复-验证闭环。

流程:  → triage(分诊:优先 L1 精确索引判定错误类别与修复策略)
      → fix(知识库/案例库检索 + LLM 结构化诊断)
      → verify(把修复代码丢进执行引擎真跑一遍)
      → 通过:交付「已验证」结果
      → 没通过:把新报错带回 fix 重新修(最多 config.MAX_FIX_ROUNDS 轮)

核心价值:AI 给出的修复代码必须经过真实执行验证,拿不到「已验证」就如实标注。

角色划分:triage / fix 是 LLM 角色(分诊在 L1 命中时不调用模型),
verify 是确定性代码环节 —— 「是否修好了」永远由真跑判定,不由模型判定。

流式支持:节点内通过 _emit() 把过程事件(检索/分析/逐字文本/验证结果)推给
外部接收器(contextvar 注入,未注入时为 no-op)。同步调用 run_diagnosis 完全不受影响。
"""

import contextvars
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

import config
import kb
import llm
import rag
import runner
import traceback_parser

# 流式事件接收器:由 run_diagnosis_stream 注入,节点内 _emit 推送(默认 no-op)
_EVENT_SINK = contextvars.ContextVar("codoctor_event_sink", default=None)


def _emit(event: dict):
    """向事件接收器推一条流程事件;没有接收器(普通调用)时静默跳过。"""
    sink = _EVENT_SINK.get()
    if sink is not None:
        try:
            sink(event)
        except Exception:
            pass


class DoctorState(TypedDict, total=False):
    code: str
    stdout: str
    stderr: str            # 当前最新一次运行的真实报错(重试轮会更新为修复版的报错)
    error: dict | None
    timed_out: bool
    round: int
    max_rounds: int
    triage: dict           # 分诊结论:error_category / repair_strategy / focus / confidence / source
    knowledge: list
    case_hits: list
    diagnosis: dict
    review: dict           # 审查结论:verdict / issues / blocking / skipped
    verify: dict | None
    verified: bool
    attempts: list


# ---------------------------------------------------------------- 分诊(Triage)

# L1 命中的错误类型 → (错误类别, 修复策略)。给的是"倾向",不是硬约束。
_L1_TRIAGE_MAP = {
    "SyntaxError": ("syntax", "minimal_fix"),
    "IndentationError": ("syntax", "minimal_fix"),
    "TabError": ("syntax", "minimal_fix"),
    "NameError": ("logic", "minimal_fix"),
    "UnboundLocalError": ("logic", "minimal_fix"),
    "TypeError": ("logic", "minimal_fix"),
    "ValueError": ("logic", "minimal_fix"),
    "IndexError": ("logic", "minimal_fix"),
    "KeyError": ("logic", "minimal_fix"),
    "AttributeError": ("logic", "minimal_fix"),
    "ZeroDivisionError": ("logic", "minimal_fix"),
    "EOFError": ("logic", "minimal_fix"),
    "ModuleNotFoundError": ("environment", "env_guide"),
    "ImportError": ("environment", "env_guide"),
    "FileNotFoundError": ("environment", "minimal_fix"),
    "UnicodeDecodeError": ("environment", "minimal_fix"),
    "RecursionError": ("logic", "logic_rewrite"),
    "__timeout__": ("logic", "logic_rewrite"),
}

_CATEGORY_LABEL = {
    "syntax": "语法", "logic": "逻辑", "environment": "环境依赖",
    "data": "数据处理", "unknown": "未分类",
}
_STRATEGY_LABEL = {
    "minimal_fix": "最小改动", "logic_rewrite": "重写逻辑", "env_guide": "环境指引",
}


def _triage_node(state: DoctorState) -> dict:
    """分诊:默认先查 L1 精确索引,命中即得结论(不调 LLM);未命中才用 LLM 兜底。

    CD_KB_MODE=vector 时改为用 L2 向量命中的条目反推错误类型;向量无召回则退回 L1。
    """
    error = state.get("error")
    timed_out = bool(state.get("timed_out"))
    etype = "__timeout__" if timed_out else (error or {}).get("type")
    message = (error or {}).get("message") or ""

    source = "l1"
    if config.KB_MODE == "vector":
        hits = []
        if config.KB_VECTOR:
            hits = kb._l2_search(
                kb.build_query(error, timed_out, state.get("stderr", ""), state.get("code", "")), 1
            )
        if hits:
            source = "l2"
    else:
        hits = kb._l1_search(etype, message)

    if hits:
        # L1 模式下命中类型必然等于 etype;向量模式以命中条目的类型为准
        hit_type = str(hits[0].get("error_type") or etype or "")
        category, strategy = _L1_TRIAGE_MAP.get(hit_type, ("unknown", "minimal_fix"))
        line = (error or {}).get("line")
        snippet = ((error or {}).get("snippet") or "").strip()
        focus = f"第 {line} 行:{snippet}" if line else str(hits[0].get("title") or "")
        triage = {
            "error_category": category,
            "repair_strategy": strategy,
            "focus": focus,
            "confidence": 0.8 if source == "l1" else 0.7,
            "source": source,
            "matched_id": hits[0].get("id"),
        }
        prefix = "已识别为" if source == "l1" else "向量匹配为"
        msg = (f"{prefix}{_CATEGORY_LABEL[category]}类错误,"
               f"修复策略:{_STRATEGY_LABEL[strategy]}")
    else:
        # L1 未命中(少见错误 / 知识库未收录)→ 交给 LLM 分诊兜底
        _emit({"type": "status", "stage": "triage",
               "msg": "知识库未精确匹配,正在用 AI 判断错误类型…"})
        got = llm.triage(
            code=state.get("code", ""),
            error=error,
            stderr=state.get("stderr", ""),
            timed_out=timed_out,
        )
        if got:
            triage = {**got, "matched_id": None}
            msg = (f"AI 判断为{_CATEGORY_LABEL.get(got.get('error_category'), '未分类')}类错误,"
                   f"修复策略:{_STRATEGY_LABEL.get(got.get('repair_strategy'), '最小改动')}")
        else:
            triage = {
                "error_category": "unknown",
                "repair_strategy": "minimal_fix",
                "focus": "",
                "confidence": 0.0,
                "source": "fallback",
                "matched_id": None,
            }
            msg = "分诊未成功,按通用策略修复"

    _emit({"type": "status", "stage": "triage", "msg": msg, "triage": triage})
    return {"triage": triage}



def _fix_node(state: DoctorState) -> dict:
    """修复:知识库 + 案例库检索 → 结构化诊断。无 Key 或 AI 失败时降级为知识库规则模式。

    分诊结论(state["triage"])在第 2 步接入 prompt;当前保持原有修复行为不变。
    """
    error = state.get("error")
    timed_out = state.get("timed_out", False)
    stderr = state.get("stderr", "")
    code = state.get("code", "")
    round_no = int(state.get("round", 0)) + 1   # 当前正在生成的轮次(从 1 开始)
    triage = state.get("triage") or {}

    _emit({"type": "status", "stage": "search", "round": round_no,
           "msg": "正在检索知识库与案例库…"})
    hits = kb.search(error=error, timed_out=timed_out, stderr=stderr, code=code)
    knowledge_text = kb.format_entries(hits)

    # 案例文档库检索(用户上传的文档;空库/失败时自动跳过,不影响主链路)
    case_hits = rag.search_cases(kb.build_query(error, timed_out, stderr, code))
    cases_text = rag.format_cases(case_hits)

    if not config.API_KEY:
        _emit({"type": "status", "stage": "analyze", "round": round_no,
               "msg": "未配置 AI,用知识库规则生成诊断…"})
        diagnosis = kb.rule_diagnosis(error, timed_out, stderr)
    else:
        _emit({"type": "status", "stage": "analyze", "round": round_no,
               "msg": f"AI 正在分析报错(第 {round_no} 轮)…"})
        try:
            diagnosis = None
            for evt in llm.diagnose_stream(
                code=code,
                error=error,
                stdout=state.get("stdout", ""),
                stderr=stderr,
                timed_out=timed_out,
                knowledge=knowledge_text,
                cases=cases_text,
                attempts=state.get("attempts") or [],
                strategy=str(triage.get("repair_strategy") or ""),
                focus=str(triage.get("focus") or ""),
            ):
                if evt.get("type") == "final":
                    diagnosis = evt.get("data")
                else:
                    _emit(evt)
            if not diagnosis:
                raise RuntimeError("流式诊断没有产出最终结果")
        except Exception as exc:
            diagnosis = kb.rule_diagnosis(error, timed_out, stderr)
            diagnosis["ai_error"] = f"调用 AI 服务失败:{exc}"

    return {"diagnosis": diagnosis, "knowledge": hits, "case_hits": case_hits}


def _critic_node(state: DoctorState) -> dict:
    """审查:在真跑之前抓「假修复」(治标 / 偏离意图 / 新风险 / 不完整)。

    审查失败一律放行(fail-open)—— verify 是确定性兜底,不该被增强环节阻断。
    """
    diagnosis = state.get("diagnosis") or {}
    fixed_code = (diagnosis.get("fixed_code") or "").strip()
    round_no = int(state.get("round", 0)) + 1

    # 规则模式 / 没有修复代码:没什么可审的,直接放行
    if not fixed_code:
        return {"review": {"verdict": "pass", "issues": [], "blocking": False, "skipped": True}}

    _emit({"type": "status", "stage": "review", "round": round_no,
           "msg": f"正在审查第 {round_no} 轮修复是否治标…"})

    got = llm.review(
        code=state.get("code", ""),
        error=state.get("error"),
        fixed_code=fixed_code,
        knowledge=kb.format_entries(state.get("knowledge") or []),
        risk_notes=str(diagnosis.get("risk_notes") or ""),
    )

    if got is None:
        _emit({"type": "status", "stage": "review_result", "round": round_no, "ok": True,
               "msg": "审查跳过(模型不可用),直接进入真实运行"})
        return {"review": {"verdict": "pass", "issues": [], "blocking": False, "skipped": True}}

    verdict = got.get("verdict") or "pass"
    issues = [str(x) for x in (got.get("issues") or [])][:5]
    review = {**got, "issues": issues, "skipped": False}

    if verdict == "pass":
        _emit({"type": "status", "stage": "review_result", "round": round_no, "ok": True,
               "msg": "审查通过,准备真实运行"})
        return {"review": review}

    head = issues[0][:90] if issues else "修复方案存在明显问题"
    _emit({"type": "status", "stage": "review_result", "round": round_no, "ok": False,
           "msg": f"审查不通过:{head}"})

    # 工程兜底:已经打回过一次,这一版仍被否 → 不再消耗轮次重写,交给真跑裁决。
    # 理由:审查是增强环节,连续两次否定同一方向说明它自己也不确定;
    # 而真跑是确定性的,误杀的代价高于漏审。
    prior_rejects = sum(1 for a in (state.get("attempts") or [])
                        if a.get("gate") == "critic")
    if prior_rejects >= 1:
        _emit({"type": "status", "stage": "review_result", "round": round_no, "ok": True,
               "msg": "审查仍有疑虑,但已重写过一版,交给真实运行裁决"})
        return {
            "review": {**review, "verdict": "pass", "blocking": False,
                       "overridden": True, "original_verdict": verdict},
        }

    # 首次打回:消耗一轮,带审查意见回修复
    attempts = list(state.get("attempts") or [])
    attempts.append({
        "round": round_no,
        "gate": "critic",
        "ok": False,
        "fixed_code": fixed_code,
        "error_summary": "审查打回:" + "；".join(issues[:3]),
        "stderr": "",
    })
    return {"review": review, "attempts": attempts, "round": round_no}


def _verify_node(state: DoctorState) -> dict:
    """把修复代码放进执行引擎真跑一遍。不通过则把新报错写回 state,供重试轮使用。"""
    diagnosis = state.get("diagnosis") or {}
    fixed_code = (diagnosis.get("fixed_code") or "").strip()
    round_no = int(state.get("round", 0)) + 1

    if not fixed_code:
        _emit({"type": "status", "stage": "verify_skip", "round": round_no,
               "msg": "本次诊断没有可验证的修复代码"})
        return {"verify": None, "verified": False}

    _emit({"type": "status", "stage": "verify", "round": round_no,
           "msg": f"正在真实运行第 {round_no} 轮修复代码…"})
    result = runner.run_python(fixed_code)
    err = traceback_parser.parse_error(result["stderr"], fixed_code)
    result["error"] = err
    ok = bool(result.get("ok")) and err is None

    if ok:
        msg = f"第 {round_no} 轮修复代码真实运行通过 ✓"
    elif err:
        msg = f"第 {round_no} 轮修复仍然报错:{err['type']}: {(err.get('message') or '')[:80]}"
    elif result.get("timed_out"):
        msg = f"第 {round_no} 轮修复运行超时"
    else:
        msg = f"第 {round_no} 轮修复运行未通过"
    _emit({"type": "status", "stage": "verify_result", "round": round_no, "ok": ok, "msg": msg})

    attempts = list(state.get("attempts") or [])
    attempts.append({
        "round": round_no,
        "gate": "verify",
        "fixed_code": fixed_code,
        "ok": ok,
        "error_summary": (
            f"{err['type']}: {err['message']}" if err
            else ("程序运行超时" if result.get("timed_out") else "")
        ),
        "stderr": (result.get("stderr") or "")[-1200:],
    })

    update = {"verify": result, "verified": ok, "attempts": attempts, "round": round_no}
    if not ok:
        update["stderr"] = result.get("stderr", "")
        update["error"] = err
        update["timed_out"] = bool(result.get("timed_out"))
    return update


def _has_budget(state: DoctorState) -> bool:
    """还能不能再产出一版修复(round = 已产出的修复版本数)。"""
    return int(state.get("round", 0)) < int(state.get("max_rounds", config.MAX_FIX_ROUNDS))


def _route_after_critic(state: DoctorState) -> str:
    """审查后的分流:通过→真跑;打回→有预算就重写,没预算如实交付;reject→直接交付解释。"""
    review = state.get("review") or {}
    verdict = review.get("verdict") or "pass"
    if verdict == "reject":
        return "done"
    if verdict == "revise":
        return "retry" if _has_budget(state) else "done"
    return "verify"


def _route_after_verify(state: DoctorState) -> str:
    if state.get("verified"):
        return "done"
    fixed_code = ((state.get("diagnosis") or {}).get("fixed_code") or "").strip()
    if not fixed_code:
        return "done"  # 规则模式没有修复代码,没什么可验证的
    return "retry" if _has_budget(state) else "done"  # 轮数用尽则如实交付


def build_graph():
    g = StateGraph(DoctorState)
    g.add_node("triage", _triage_node)
    g.add_node("fix", _fix_node)
    g.add_node("critic", _critic_node)
    g.add_node("verify", _verify_node)
    g.add_edge(START, "triage")
    g.add_edge("triage", "fix")
    g.add_edge("fix", "critic")
    g.add_conditional_edges(
        "critic", _route_after_critic,
        {"retry": "fix", "verify": "verify", "done": END},
    )
    g.add_conditional_edges("verify", _route_after_verify, {"retry": "fix", "done": END})
    return g.compile()


GRAPH = build_graph()


def _new_state(code: str, error: dict | None, stdout: str, stderr: str, timed_out: bool) -> DoctorState:
    return {
        "code": code,
        "stdout": stdout,
        "stderr": stderr,
        "error": error,
        "timed_out": timed_out,
        "round": 0,
        "max_rounds": config.MAX_FIX_ROUNDS,
        "attempts": [],
    }


def _deliver(final: dict) -> dict:
    """把图终态整理成交付用的诊断结果(含 verified / verify_rounds 等)。"""
    diagnosis = dict(final.get("diagnosis") or {})
    diagnosis["verified"] = bool(final.get("verified"))
    diagnosis["verify_rounds"] = int(final.get("round", 0))
    diagnosis["triage"] = final.get("triage") or {}
    diagnosis["review"] = final.get("review") or {}
    diagnosis["knowledge_ids"] = [e["id"] for e in (final.get("knowledge") or [])]
    diagnosis["case_sources"] = [
        {"filename": c.get("filename"), "title": c.get("title"), "distance": c.get("distance")}
        for c in (final.get("case_hits") or [])
    ]

    verify = final.get("verify") or {}
    if final.get("verified"):
        diagnosis["verify_output"] = (verify.get("stdout") or "").strip()[:2000]

    diagnosis["attempts"] = [
        {
            "round": a.get("round"),
            "gate": a.get("gate", "verify"),
            "ok": a.get("ok"),
            "error": a.get("error_summary", ""),
        }
        for a in (final.get("attempts") or [])
    ]
    return diagnosis


def run_diagnosis(code: str, error: dict | None = None, stdout: str = "",
                  stderr: str = "", timed_out: bool = False) -> dict:
    """执行整张图,返回交付用的诊断结果(含 verified / verify_rounds)。"""
    final = GRAPH.invoke(
        _new_state(code, error, stdout, stderr, timed_out),
        config={"recursion_limit": config.MAX_FIX_ROUNDS * 3 + 8},
    )
    return _deliver(final)


def run_diagnosis_stream(code: str, error: dict | None = None, stdout: str = "",
                         stderr: str = "", timed_out: bool = False, sink=None) -> dict:
    """带事件流的整图执行:过程事件经 sink 回调实时推送,返回最终诊断结果。

    sink 在线程内同步调用,推什么就流什么:
    - status:阶段变化(检索/分析/验证/验证结果,含轮次)
    - field/delta/item:LLM 诊断文本的实时增量
    """
    token = _EVENT_SINK.set(sink) if sink is not None else None
    try:
        final = GRAPH.invoke(
            _new_state(code, error, stdout, stderr, timed_out),
            config={"recursion_limit": config.MAX_FIX_ROUNDS * 3 + 8},
        )
    finally:
        if token is not None:
            _EVENT_SINK.reset(token)
    return _deliver(final)
