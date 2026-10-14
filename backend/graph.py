"""LangGraph 诊断-修复-验证闭环。

流程:diagnose(知识库检索 + LLM 结构化诊断)
      → verify(把修复代码丢进执行引擎真跑一遍)
      → 通过:交付「已验证」结果
      → 没通过:把新报错带回 diagnose 重新修(最多 config.MAX_FIX_ROUNDS 轮)

核心价值:AI 给出的修复代码必须经过真实执行验证,拿不到「已验证」就如实标注。

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
    knowledge: list
    case_hits: list
    diagnosis: dict
    verify: dict | None
    verified: bool
    attempts: list


def _diagnose_node(state: DoctorState) -> dict:
    """知识库 + 案例库检索 → 结构化诊断。无 Key 或 AI 失败时降级为知识库规则模式。"""
    error = state.get("error")
    timed_out = state.get("timed_out", False)
    stderr = state.get("stderr", "")
    code = state.get("code", "")
    round_no = int(state.get("round", 0)) + 1   # 当前正在生成的轮次(从 1 开始)

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


def _route_after_verify(state: DoctorState) -> str:
    if state.get("verified"):
        return "done"
    fixed_code = ((state.get("diagnosis") or {}).get("fixed_code") or "").strip()
    if not fixed_code:
        return "done"  # 规则模式没有修复代码,没什么可验证的
    if int(state.get("round", 0)) >= int(state.get("max_rounds", config.MAX_FIX_ROUNDS)):
        return "done"  # 轮数用尽,如实交付
    return "retry"


def build_graph():
    g = StateGraph(DoctorState)
    g.add_node("diagnose", _diagnose_node)
    g.add_node("verify", _verify_node)
    g.add_edge(START, "diagnose")
    g.add_edge("diagnose", "verify")
    g.add_conditional_edges("verify", _route_after_verify, {"retry": "diagnose", "done": END})
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
    diagnosis["knowledge_ids"] = [e["id"] for e in (final.get("knowledge") or [])]
    diagnosis["case_sources"] = [
        {"filename": c.get("filename"), "title": c.get("title"), "distance": c.get("distance")}
        for c in (final.get("case_hits") or [])
    ]

    verify = final.get("verify") or {}
    if final.get("verified"):
        diagnosis["verify_output"] = (verify.get("stdout") or "").strip()[:2000]

    diagnosis["attempts"] = [
        {"round": a.get("round"), "ok": a.get("ok"), "error": a.get("error_summary", "")}
        for a in (final.get("attempts") or [])
    ]
    return diagnosis


def run_diagnosis(code: str, error: dict | None = None, stdout: str = "",
                  stderr: str = "", timed_out: bool = False) -> dict:
    """执行整张图,返回交付用的诊断结果(含 verified / verify_rounds)。"""
    final = GRAPH.invoke(
        _new_state(code, error, stdout, stderr, timed_out),
        config={"recursion_limit": config.MAX_FIX_ROUNDS * 2 + 6},
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
            config={"recursion_limit": config.MAX_FIX_ROUNDS * 2 + 6},
        )
    finally:
        if token is not None:
            _EVENT_SINK.reset(token)
    return _deliver(final)
