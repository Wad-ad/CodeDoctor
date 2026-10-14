"""多轮追问:会话上下文 + 意图识别 + 替代方案执行与结构化解释。

用户在一次诊断之后可以继续追问:
- 普通问题  → 结合完整上下文(代码 / 真实报错 / 诊断结果 / 历史对话)自然回答(流式);
- 替代方案  → 出现「为什么不那样改」「为什么不用那个方案」「能不能用 X」等表述时,
  自动识别为用户提出了另一种修改思路,进入方案重执行流程:
      按用户思路生成代码 → 真实运行验证(最多 CD_CHAT_ALT_ROUNDS 轮)
        通过 → 交付结果,并把该方案更新为当前采纳方案(上下文同步)
        未通过 / 无法实现 → 基于真实报错生成结构化解析:
             具体原因(reasons) / 技术限制(limits) / 潜在问题(issues) / 建议(suggestion)

会话保存在进程内存里,对话历史完整保留;所有过程事件通过 sink 回调实时推送。
"""

import re
import threading
import time
from dataclasses import dataclass, field

import config
import llm
import runner
import traceback_parser

_MAX_SESSIONS = 200


# ============================================================ 会话存储

@dataclass
class ChatSession:
    """一次「诊断」或「跑通」对应的追问会话:保存完整上下文与对话历史。"""

    session_id: str
    code: str = ""
    stdout: str = ""
    stderr: str = ""
    error: dict | None = None
    timed_out: bool = False
    diagnosis: dict = field(default_factory=dict)   # 该次诊断的完整结果(跑通场景为空)
    prior: dict = field(default_factory=dict)       # 跑通场景:上一轮诊断上下文(旧代码/旧报错/旧诊断)
    chosen_fix: str = ""                            # 当前采纳的修复代码(替代方案通过后会更新)
    alternatives: list = field(default_factory=list)  # 已尝试过的替代方案记录
    messages: list = field(default_factory=list)    # [{role, text, kind, meta}] 完整对话链
    busy: bool = False
    created_at: float = field(default_factory=time.time)


_SESSIONS: dict[str, ChatSession] = {}
_STORE_LOCK = threading.Lock()


def get_or_create_session(session_id: str, context: dict | None,
                          history: list | None = None) -> ChatSession:
    """按 session_id 取会话;不存在则用前端带过来的上下文创建(含历史恢复)。"""
    with _STORE_LOCK:
        session = _SESSIONS.get(session_id)
        if session is not None:
            return session

        context = context or {}
        diagnosis = context.get("diagnosis") or {}
        if not isinstance(diagnosis, dict):
            diagnosis = {}
        prior = context.get("prior") or {}
        if not isinstance(prior, dict):
            prior = {}
        session = ChatSession(
            session_id=session_id,
            code=context.get("code", "") or "",
            stdout=context.get("stdout", "") or "",
            stderr=context.get("stderr", "") or "",
            error=context.get("error"),
            timed_out=bool(context.get("timed_out")),
            diagnosis=diagnosis,
            prior=prior,
            chosen_fix=str(diagnosis.get("fixed_code") or ""),
        )
        # 后端重启等情况:用前端保留的历史消息恢复对话链(不丢上下文)
        for m in (history or [])[-40:]:
            if (isinstance(m, dict) and m.get("role") in ("user", "assistant")
                    and str(m.get("text") or "").strip()):
                session.messages.append({
                    "role": m["role"],
                    "text": str(m["text"])[:4000],
                    "kind": "text",
                })

        # 简单的容量保护:超过上限时丢弃最旧的会话
        if len(_SESSIONS) >= _MAX_SESSIONS:
            oldest = min(_SESSIONS.values(), key=lambda s: s.created_at)
            if oldest is not session:
                _SESSIONS.pop(oldest.session_id, None)
        _SESSIONS[session_id] = session
        return session


# ============================================================ 上下文组装

def _context_block(s: ChatSession) -> str:
    """把会话里的全部事实组装成给模型看的上下文块(代码/报错/诊断/已试方案)。"""
    parts = ["【原始代码(运行失败的那段)】", "```python", s.code[:6000], "```", ""]
    if s.timed_out:
        parts += ["【运行结果】程序运行超时,被强制终止。", ""]
    else:
        parts += ["【程序输出(stdout)】", (s.stdout.strip() or "(无输出)")[:1500], ""]
    if s.stderr.strip():
        parts += ["【真实报错(stderr 原文)】", s.stderr.strip()[:2000], ""]
    err = s.error or {}
    if err:
        loc = f"第 {err['line']} 行" if err.get("line") else "位置未知"
        parts += [f"【报错定位】{loc}:{err.get('type')}: {err.get('message')}", ""]

    d = s.diagnosis or {}
    if d:
        parts += ["【CodeDoctor 已有的诊断结果】"]
        if d.get("explain"):
            parts.append("· 发生了什么:" + str(d["explain"]))
        if d.get("reason"):
            parts.append("· 为什么会错:" + str(d["reason"]))
        for fp in (d.get("fix_points") or [])[:6]:
            parts.append("· 修改点:" + str(fp))
        if d.get("fixed_code"):
            verified = "已通过真实运行验证" if d.get("verified") else "未通过验证"
            parts += [f"· 修复方案({verified}):", "```python", str(d["fixed_code"])[:6000], "```"]
        if d.get("verify_output"):
            parts.append("· 验证运行输出:" + str(d["verify_output"]).strip()[:300])
        parts.append("")

    # 跑通场景:带上上一轮诊断(旧代码/旧报错/旧诊断),支撑「为什么这样改就好了」类追问
    prior = s.prior or {}
    pd = prior.get("diagnosis") or {}
    if isinstance(pd, dict) and pd:
        parts += ["【上一轮诊断(针对修改前的旧代码;用户可能问「为什么这样改就好了」之类,可参考)】"]
        old_code = str(prior.get("code") or "").strip()
        if old_code and old_code != s.code.strip():
            parts += ["· 修改前的代码:", "```python", old_code[:4000], "```"]
        if prior.get("stderr"):
            parts.append("· 修改前运行的真实报错:" + str(prior["stderr"]).strip()[:800])
        if pd.get("explain"):
            parts.append("· 当时诊断(发生了什么):" + str(pd["explain"]))
        if pd.get("reason"):
            parts.append("· 当时诊断(为什么):" + str(pd["reason"]))
        if pd.get("fixed_code"):
            parts += ["· 当时给出的修复方案:", "```python", str(pd["fixed_code"])[:4000], "```"]
        parts.append("")

    base_fix = str((s.diagnosis or {}).get("fixed_code") or "").strip()
    if s.chosen_fix.strip() and s.chosen_fix.strip() != base_fix:
        parts += ["【当前采纳的修复代码(替代方案已通过验证,以此为准)】",
                  "```python", s.chosen_fix[:6000], "```", ""]

    if s.alternatives:
        parts += ["【已尝试过的替代方案(避免重复建议)】"]
        for a in s.alternatives:
            status = "验证通过" if a.get("ok") else ("无法执行" if a.get("infeasible") else "验证未通过")
            note = f" — {a.get('summary')}" if a.get("summary") else ""
            parts.append(f"· 「{a.get('approach', '?')}」:{status}{note}")
        parts.append("")
    return "\n".join(parts)


def _history_messages(s: ChatSession) -> list:
    """把会话消息转成 LangChain 聊天消息;执行记录附在助手消息后,保持完整上下文。"""
    out = []
    for m in s.messages:
        if m["role"] == "user":
            out.append(("human", m["text"]))
        else:
            text = m.get("text") or ""
            if m.get("meta"):
                text = (text + "\n[执行记录] " + m["meta"]).strip()
            out.append(("ai", text or "(无文字回复)"))
    return out


def _history_brief(messages: list, limit: int = 8) -> str:
    lines = []
    for m in messages[-limit:]:
        who = "用户" if m["role"] == "user" else "助手"
        text = (m.get("text") or "").replace("\n", " ").strip()
        if len(text) > 160:
            text = text[:160] + "…"
        lines.append(f"{who}:{text}")
    return "\n".join(lines) or "(无)"


def _clean_reply(text: str) -> str:
    """去掉模型可能带出的 markdown 强调/标题标记,保留可读的列表。"""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text or "")
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    return text.strip()


def _as_list(value) -> list:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


# ============================================================ LLM 小工具

def _json_call(system: str, user: str) -> dict:
    """一次结构化 JSON 调用:优先 JSON 强制模式,失败退普通模式 + 手动抽取。"""
    try:
        reply = llm.build_stream_model(json_mode=True).invoke(
            [("system", system), ("human", user)])
    except Exception:
        reply = llm.build_stream_model(json_mode=False).invoke(
            [("system", system), ("human", user)])
    return llm.extract_json(llm.chunk_text(reply))


def _stream_reply(msgs: list, sink) -> str:
    """流式生成自然语言回复:逐段 emit delta,返回完整文本。"""
    parts = []
    model = llm.build_stream_model(json_mode=False)
    for chunk in model.stream(msgs):
        text = llm.chunk_text(chunk)
        if text:
            parts.append(text)
            sink({"type": "delta", "text": text})
    return _clean_reply("".join(parts))


# ============================================================ 意图识别

_CLASSIFY_SYSTEM = """你是 CodeDoctor 对话系统的意图识别器。

判断用户最新一条消息属于哪类:
- alternative:用户在提出或询问「另一套尚未实施的修改 / 实现思路」,潜台词是「我想试试这样做,行不行」。
  例:「为什么不那样改?」「为什么不用那个方案?」「为什么不用 try/except?」「换成 while 循环试试」
     「能不能用 len 直接算?」「我觉得用字典更好」「应该用 sum 除以 len」「为什么不直接删掉那一行」
- question:其余情况——想弄明白「已经写好 / 已经发生的事」的原理,或询问含义、细节、建议、闲聊。
  例:「这个错误常见吗?」「ZeroDivisionError 是什么意思?」「为什么把 count 改成 len(scores) 之后就不报错了?」
     「现在这版还能怎么优化?」「以后怎么避免?」「谢谢!」

判断要点:消息是否指向一个目前还没有实现、值得实际试一下的具体改法?
  · 是 → alternative(哪怕用「为什么不用」这种提问语气);
  · 只是问已经写好 / 已经发生的事为什么行(不行)→ question。
  · 「为什么把 X 改成 Y 之后就报错 / 就不报错了」「为什么这样写能跑通」这类句子是在问原理(改动已发生)→ question。

输出 JSON:{"intent": "alternative" 或 "question", "proposal": "若为 alternative,用一句话概括用户想要采用的替代思路(保留用户原意,包含具体方法);否则为空字符串"}
只输出 JSON,不要其他内容。"""

_ALT_HINTS = ("为什么不那样", "为什么不直接", "为什么不用", "为什么没有用", "换成", "改成",
              "改用", "试试用", "试试改", "能不能用", "应该用")


def _looks_like_alternative(message: str) -> bool:
    return any(h in message for h in _ALT_HINTS)


def classify_intent(s: ChatSession, message: str) -> dict:
    """识别意图。LLM 失败时退化为关键词判断(仅作兜底)。"""
    user = "\n".join([
        "当前场景:用户有一段 Python 代码(可能运行失败、已有诊断;也可能已经跑通),正在与助手继续讨论。",
        "【最近的对话】",
        _history_brief(s.messages[:-1], 8),
        "",
        "【用户最新消息】",
        message,
    ])
    try:
        data = _json_call(_CLASSIFY_SYSTEM, user)
        intent = str(data.get("intent", "")).strip().lower()
        if intent not in ("alternative", "question"):
            raise ValueError(f"未知意图:{intent}")
        proposal = str(data.get("proposal") or "").strip()
        if intent == "alternative" and not proposal:
            proposal = message
        return {"intent": intent, "proposal": proposal}
    except Exception:
        if _looks_like_alternative(message):
            return {"intent": "alternative", "proposal": message}
        return {"intent": "question", "proposal": ""}


# ============================================================ 替代方案:生成 → 真实运行

_ALT_SYSTEM = """你是 CodeDoctor 的「替代方案执行器」。用户正在和你讨论一段 Python 代码(可能运行失败、已有诊断;也可能已经跑通),并提出了另一种修改思路。你的任务:严格按用户的思路,基于现有代码,写出完整可运行的替代版本代码,交给系统真实运行验证。

要求:
1. 忠实于用户提出的思路,不要替换成你自己偏好的方案;若用户的思路需要小幅调整才能落地,尽量贴近其本意。
2. 若用户的思路与现有代码本质相同,直接生成对应的完整代码即可。
3. 输出 JSON:{"feasible": true/false, "approach": "该方案的一句话名称(如:用 len(scores) 直接计算平均)", "code": "完整可运行的代码", "reason": "若 feasible=false,说明该思路为什么无法写成代码;否则为空字符串"}
4. 只有当该思路在 Python 语言层面根本不存在或明显自相矛盾(如要求 goto、要求不存在的机制)时才 feasible=false;
   环境缺库、需要绕弯、效率不佳等情况请照常给出代码——「能不能跑通」由真实运行来判定,不要主观预测。
5. code 必须是完整代码,最大程度保留用户原有结构和注释,只按新思路做修改;不要 markdown 围栏。"""


def _generate_alternative(s: ChatSession, proposal: str, attempts: list) -> dict:
    parts = [
        _context_block(s),
        "【最近对话】",
        _history_brief(s.messages[:-1], 6),
        "",
        "【用户提出的替代思路】",
        proposal,
    ]
    if attempts:
        parts += ["", "【历次尝试(都没跑通,请修正实现细节;用户思路保持不变)】",
                  "注意:不得把用户指定的库或机制替换成其他东西;若真实报错表明用户指定的机制"
                  "在当前环境或语言中不可用,请把 feasible 设为 false,并在 reason 里引用真实报错。"]
        for a in attempts:
            parts += [f"--- 第 {a['round']} 轮:代码未通过真实运行", "```python",
                      a.get("code", ""), "```", f"真实报错:{a.get('error_summary', '')}",
                      (a.get("stderr") or "")[:600]]
    parts += ["", "请输出 JSON:feasible / approach / code / reason。"]

    data = _json_call(_ALT_SYSTEM, "\n".join(parts))
    code = llm.strip_code_fence(str(data.get("code") or ""))
    feasible = bool(data.get("feasible", True))
    reason = str(data.get("reason") or "").strip()
    if feasible and not code:
        feasible, reason = False, reason or "模型没有产出可运行的代码"
    return {
        "feasible": feasible,
        "approach": str(data.get("approach") or "").strip() or proposal,
        "code": code,
        "reason": reason,
    }


def _missing_module(err: dict | None) -> str | None:
    """从真实报错里提取「缺哪个模块」;不是导入类报错则返回 None。"""
    if not err or err.get("type") not in ("ModuleNotFoundError", "ImportError"):
        return None
    m = re.search(r"No module named ['\"]([^'\"]+)['\"]", err.get("message") or "")
    return m.group(1) if m else None


def _mentions_module(s: ChatSession, proposal: str, module: str) -> bool:
    """用户是否点到了这个模块(在替代思路或最新消息里出现)。"""
    base = module.split(".")[0].lower()
    last_user = s.messages[-1].get("text", "") if s.messages else ""
    return base in (proposal + " " + last_user).lower()


def run_alternative(s: ChatSession, proposal: str, sink) -> tuple:
    """完整执行替代方案:生成 → 真实运行 → 通过交付 / 失败结构化解释。

    返回 (payload, text):payload 为交付给前端的结构化结果,text 为该轮对话文字。
    """
    max_rounds = max(1, config.CHAT_ALT_ROUNDS)
    attempts: list = []
    approach = proposal
    result: dict = {"ok": False, "approach": approach, "code": "", "stdout": "", "attempts": attempts}

    for rnd in range(1, max_rounds + 1):
        if rnd == 1:
            sink({"type": "status", "stage": "generate", "round": rnd,
                  "msg": "正在按你的思路生成替代方案代码…"})
        else:
            sink({"type": "status", "stage": "revise", "round": rnd,
                  "msg": f"这版没跑通,根据真实报错调整后重试(第 {rnd} 轮)…"})

        try:
            gen = _generate_alternative(s, proposal, attempts)
        except Exception as exc:
            attempts.append({"round": rnd, "code": "", "ok": False, "infeasible": True,
                             "error_summary": f"生成代码失败:{exc}", "stderr": ""})
            result = {"ok": False, "approach": approach, "code": "", "stdout": "", "attempts": attempts}
            break

        approach = gen["approach"] or approach
        result["approach"] = approach

        if not gen["feasible"]:
            attempts.append({"round": rnd, "code": "", "ok": False, "infeasible": True,
                             "error_summary": "该思路无法写成可执行代码",
                             "infeasible_reason": gen["reason"], "stderr": ""})
            result = {"ok": False, "approach": approach, "code": "", "stdout": "", "attempts": attempts}
            break

        code = gen["code"]
        sink({"type": "status", "stage": "run", "round": rnd,
              "msg": f"正在真实运行这版代码(第 {rnd} 轮)…"})
        run = runner.run_python(code)
        err = traceback_parser.parse_error(run["stderr"], code)
        ok = bool(run.get("ok")) and err is None

        attempts.append({
            "round": rnd, "code": code, "ok": ok, "infeasible": False,
            "error_summary": (f"{err['type']}: {err['message']}" if err
                              else ("程序运行超时" if run.get("timed_out") else "")),
            "stderr": (run.get("stderr") or "")[-1500:],
        })

        if ok:
            sink({"type": "status", "stage": "verify_result", "round": rnd, "ok": True,
                  "msg": f"第 {rnd} 轮替代方案真实运行通过 ✓"})
            result = {"ok": True, "approach": approach, "code": code,
                      "stdout": (run.get("stdout") or "").strip()[:2000], "attempts": attempts}
            break

        detail = f"{err['type']}: {err['message']}" if err else "运行超时"
        sink({"type": "status", "stage": "verify_result", "round": rnd, "ok": False,
              "msg": f"第 {rnd} 轮没跑通:{detail}"})
        result = {"ok": False, "approach": approach, "code": code, "stdout": "", "attempts": attempts}

        # 用户点名的库在当前环境不可用 → 这不是实现细节问题,直接作为「无法执行」证据,不再重试
        missing = _missing_module(err)
        if missing and _mentions_module(s, proposal, missing):
            attempts[-1]["infeasible"] = True
            attempts[-1]["infeasible_reason"] = (
                f"运行环境里没有安装「{missing}」(真实报错:{err['type']}: {err['message']})")
            sink({"type": "status", "stage": "verify_result", "round": rnd, "ok": False,
                  "msg": f"这个方案依赖的「{missing}」在当前环境不可用,不再重试"})
            break

    if result["ok"]:
        # ---- 成功:自然总结 + 更新会话状态(该方案成为当前采纳方案) ----
        text = _success_summary(s, proposal, result, sink)
        s.chosen_fix = result["code"]
        s.alternatives.append({
            "approach": approach, "ok": True,
            "summary": f"真实运行输出:{result['stdout'][:120] or '(无输出)'}",
        })
        payload = {
            "kind": "alt_success",
            "approach": approach,
            "code": result["code"],
            "stdout": result["stdout"],
            "rounds": len(attempts),
            "text": text,
            "attempts": [{"round": a["round"], "ok": a["ok"], "error": a["error_summary"]}
                         for a in attempts],
        }
        return payload, text

    # ---- 失败:基于真实证据生成结构化解析 ----
    sink({"type": "status", "stage": "explain", "round": len(attempts),
          "msg": "正在整理这个方案为什么走不通…"})
    explain = _explain_failure(s, proposal, attempts)
    infeasible = bool(attempts) and bool(attempts[-1].get("infeasible"))
    s.alternatives.append({
        "approach": approach, "ok": False, "infeasible": infeasible,
        "summary": explain.get("summary", ""),
    })
    payload = {
        "kind": "alt_fail",
        "approach": approach,
        "infeasible": infeasible,
        "summary": explain.get("summary", ""),
        "reasons": explain.get("reasons", []),
        "limits": explain.get("limits", []),
        "issues": explain.get("issues", []),
        "suggestion": explain.get("suggestion", ""),
        "attempts": [{"round": a["round"], "ok": a["ok"], "error": a["error_summary"],
                      "infeasible": bool(a.get("infeasible"))} for a in attempts],
    }
    return payload, explain.get("summary", "")


_SUCCESS_SYSTEM = """你是 CodeDoctor。用户提出了一个替代修改思路,系统已经按其思路生成代码并真实运行验证通过。
请用自然、口语化的两三句话(中文)告诉用户这个好消息:①已按他/她的思路试过并真实跑通;②运行输出是什么;③与现有写法(或之前的诊断方案)相比有什么值得说明的(等价 / 更简洁 / 更贴近原有习惯),没有就不提。
不要用 markdown 标题或加粗,不要复述代码全文,直接说话,不超过 150 字。"""


def _success_summary(s: ChatSession, proposal: str, result: dict, sink) -> str:
    user = "\n".join([
        "【用户提出的替代思路】", proposal,
        "【已验证通过的替代代码】", "```python", result["code"], "```",
        "【真实运行输出】", result["stdout"] or "(无输出)",
        "", "请用两三句话自然地说给用户听。",
    ])
    try:
        text = _stream_reply([("system", _SUCCESS_SYSTEM), ("human", user)], sink)
    except Exception:
        text = ""
    return text or f"我按「{result['approach']}」的思路改了一版,真实运行通过了。输出:{result['stdout'] or '(无输出)'}"


_EXPLAIN_SYSTEM = """你是 CodeDoctor,一名耐心、友善的编程助教。用户提出了一个替代修改思路,系统尝试真实执行它但没有成功(或该思路根本无法写成代码)。
请生成一份清晰易懂的结构化解析(面向编程初学者),说明这个方案为什么走不通。要求:
1. 客观、友善:如果用户的思路里有合理的部分,先肯定,再解释问题;不要打击用户。
2. 具体原因必须引用【尝试记录与真实证据】中的真实报错,不要编造。
3. 输出 JSON:{"summary": "一两句口语化的总结(自然,像说话,直接说为什么走不通)", "reasons": ["无法执行的具体原因,引用真实报错证据"], "limits": ["相关的技术限制(语言机制、环境、依赖等)"], "issues": ["如果强行按这个方向改,会带来的潜在问题或副作用(没有就留空数组)"], "suggestion": "接下来怎么办:现有方案为什么更合适,或如何变通实现用户想法的一部分"}"""


def _explain_failure(s: ChatSession, proposal: str, attempts: list) -> dict:
    evidence = []
    for a in attempts:
        if a.get("infeasible"):
            evidence += [f"--- 第 {a['round']} 轮:该思路无法执行",
                         f"原因:{a.get('infeasible_reason') or a.get('error_summary')}"]
            if a.get("stderr"):
                evidence += [f"真实报错原文:{a['stderr'][:500]}"]
        else:
            evidence += [f"--- 第 {a['round']} 轮的代码", "```python", a.get("code", ""), "```",
                         f"真实运行报错:{a.get('error_summary')}",
                         (a.get("stderr") or "")[:800]]
    user = "\n".join([
        _context_block(s),
        "【用户提出的替代思路】", proposal,
        "【尝试记录与真实证据】", "\n".join(evidence) or "(无)",
        "", "请输出 JSON。",
    ])
    try:
        data = _json_call(_EXPLAIN_SYSTEM, user)
        out = {
            "summary": str(data.get("summary") or "").strip(),
            "reasons": _as_list(data.get("reasons")),
            "limits": _as_list(data.get("limits")),
            "issues": _as_list(data.get("issues")),
            "suggestion": str(data.get("suggestion") or "").strip(),
        }
        if not out["summary"]:
            out["summary"] = "这个方案目前走不通,具体原因见下。"
        return out
    except Exception:
        return _rule_explain(attempts)


def _rule_explain(attempts: list) -> dict:
    """解释服务不可用时的兜底:只用真实运行证据说话。"""
    last = attempts[-1] if attempts else {}
    if last.get("infeasible"):
        why = last.get("infeasible_reason") or last.get("error_summary") or "该做法在 Python 中无法直接实现"
        return {"summary": f"这个思路没法直接写成 Python 代码:{why}",
                "reasons": [why], "limits": [], "issues": [], "suggestion": ""}
    detail = last.get("error_summary") or "仍有报错"
    reasons = [f"真实运行报错:{detail}"]
    if last.get("stderr"):
        reasons.append("报错原文:" + (last["stderr"] or "").strip()[-400:])
    return {"summary": f"按这个方案真实运行了 {len(attempts)} 轮,仍然没有通过:{detail}",
            "reasons": reasons, "limits": [], "issues": [],
            "suggestion": "可以换一种思路再问我,或者让我解释现有方案为什么更合适。"}


# ============================================================ 普通追问(自然回答)

_QA_SYSTEM = """你是 CodeDoctor,一名耐心、友善的编程助教,正在和使用者继续讨论上面这段 Python 代码。
这段代码可能运行失败、已有诊断,也可能已经跑通;如果上下文里带有「上一轮诊断」信息,回答「为什么这样改就好了」之类的问题时请一并参考。

要求:
1. 用自然、口语化的中文直接回答(像面对面聊天),通俗优先;必须使用专业词时紧跟一句通俗解释。
2. 回答紧扣上下文(原始代码、真实报错、诊断结果、之前聊过的内容);引用具体行号、变量名会让回答更清楚。
3. 不要用 markdown 标题或加粗语法;需要列举时用「1. 2. 3.」或「-」即可。
4. 除非用户要求详细展开,控制在 350 字以内,别啰嗦。
5. 如果用户问的与代码无关,也友好自然地回应。"""


def run_question(s: ChatSession, sink) -> tuple:
    sink({"type": "status", "stage": "answer", "msg": "正在结合上下文回答…"})
    msgs = [("system", _QA_SYSTEM + "\n\n" + _context_block(s))]
    msgs += _history_messages(s)
    try:
        text = _stream_reply(msgs, sink)
    except Exception as exc:
        text = f"抱歉,我这边回答时出了点问题({exc}),换个问法再试试?"
    if not text:
        text = "抱歉,我没有生成出有效回复,换个问法再试试?"
    return {"kind": "text", "text": text}, text


# ============================================================ 会话入口

def _meta_for(payload: dict) -> str:
    """给助手消息附一段执行记录,让后续轮次的模型能看到完整事实。"""
    kind = payload.get("kind")
    if kind == "alt_success":
        out = (payload.get("stdout") or "").strip()[:150] or "(无输出)"
        return (f"已按「{payload.get('approach')}」生成代码并真实运行验证通过,"
                f"输出:{out};该方案已更新为当前推荐方案。")
    if kind == "alt_fail":
        return f"替代方案「{payload.get('approach')}」无法执行:{payload.get('summary', '')}"
    return ""


def handle_message(session_id: str, message: str, context: dict | None, sink,
                   history: list | None = None) -> None:
    """处理一轮追问:整个流程的事件经 sink 实时推送,最后给出 result。"""
    message = (message or "").strip()
    if not message:
        sink({"type": "error", "message": "消息是空的"})
        return

    session = get_or_create_session(session_id, context, history)
    if session.busy:
        sink({"type": "error", "message": "上一条消息还在处理中,请稍等它完成"})
        return

    session.busy = True
    try:
        sink({"type": "session", "session_id": session_id})
        session.messages.append({"role": "user", "text": message})

        if not config.API_KEY:
            text = "当前后端没有配置 AI 服务(CD_API_KEY 为空),暂时没法进行追问对话。"
            payload = {"kind": "text", "text": text}
        else:
            sink({"type": "status", "stage": "classify", "msg": "正在理解你的问题…"})
            cls = classify_intent(session, message)
            if cls.get("intent") == "alternative":
                payload, text = run_alternative(session, cls.get("proposal") or message, sink)
            else:
                payload, text = run_question(session, sink)

        session.messages.append({
            "role": "assistant",
            "text": text,
            "kind": payload.get("kind", "text"),
            "meta": _meta_for(payload),
        })
        sink({"type": "result", "data": payload})
    except Exception as exc:
        err_text = f"这次追问处理时出了点问题:{type(exc).__name__}: {exc}"
        sink({"type": "error", "message": err_text})
        session.messages.append({"role": "assistant", "text": err_text, "kind": "text"})
    finally:
        session.busy = False
