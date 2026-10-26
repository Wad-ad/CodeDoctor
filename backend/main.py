"""CodeDoctor 后端入口:代码真实执行 + AI 诊断 API。

启动:uvicorn main:app --host 127.0.0.1 --port 8000
"""

import json
import queue
import threading

from fastapi import FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import config
import chat
import doctor
import graph
import kb
import rag
import runner
import traceback_parser

app = FastAPI(title="CodeDoctor API", description="代码报错诊断助手", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class CodeRequest(BaseModel):
    code: str


class DiagnoseRequest(BaseModel):
    code: str
    stdout: str = ""
    stderr: str = ""
    error: dict | None = None
    timed_out: bool = False


@app.get("/api/health")
def health():
    """健康检查:同时告诉前端 AI 是否已配置。"""
    return {
        "status": "ok",
        "ai_configured": bool(config.API_KEY),
        "model": config.MODEL if config.API_KEY else None,
        "exec_timeout": config.EXEC_TIMEOUT,
        "kb_entries": len(kb.load_entries()),
        "kb_mode": config.KB_MODE,
        "kb_backend": kb.backend_status(),
        "kb_cases": rag.stats(),
        "max_fix_rounds": config.MAX_FIX_ROUNDS,
        "chat_alt_rounds": config.CHAT_ALT_ROUNDS,
    }


@app.post("/api/run")
def run_code(req: CodeRequest):
    """第一步:真实执行代码,捕获输出与报错,并解析出报错行号。"""
    result = runner.run_python(req.code)
    result["error"] = traceback_parser.parse_error(result["stderr"], req.code)
    return result


@app.post("/api/diagnose")
def diagnose_code(req: DiagnoseRequest):
    """第二步:基于第一步的真实运行结果做 AI 诊断(不重复执行代码)。"""
    if not req.stderr.strip() and not req.timed_out:
        return {"ok": True, "message": "代码运行通过,没有发现需要诊断的报错。"}
    diagnosis = doctor.diagnose(req.code, req.error, req.stdout, req.stderr, req.timed_out)
    return {"ok": False, "diagnosis": diagnosis}


# ---------------------------------------------------------------- 流式诊断(SSE)

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",   # 若经反向代理,禁用其缓冲
}


def _sse(event: dict) -> str:
    """把一条事件序列化成 SSE 数据帧(JSON 无裸换行,单行安全)。"""
    return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"


@app.post("/api/diagnose/stream")
def diagnose_stream(req: DiagnoseRequest):
    """流式诊断:SSE 实时推送过程事件,最后给完整结果。

    事件类型:
      status        阶段变化(检索/分析/验证/验证结果,带轮次)
      field/delta   诊断文本实时增量(explain/reason/fix_points/fixed_code)
      item          fix_points 单条完成
      result        最终完整诊断数据
      error         链路异常
      done          结束标记
    """
    if not req.stderr.strip() and not req.timed_out:
        def _noop():
            yield _sse({"type": "result", "data": None,
                        "message": "代码运行通过,没有发现需要诊断的报错。"})
            yield _sse({"type": "done"})
        return StreamingResponse(_noop(), media_type="text/event-stream", headers=_SSE_HEADERS)

    def gen():
        events: "queue.Queue" = queue.Queue()

        def worker():
            try:
                result = graph.run_diagnosis_stream(
                    code=req.code,
                    error=req.error,
                    stdout=req.stdout,
                    stderr=req.stderr,
                    timed_out=req.timed_out,
                    sink=events.put,
                )
                events.put({"type": "result", "data": result})
            except Exception as exc:
                events.put({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            finally:
                events.put(None)   # 结束哨兵

        threading.Thread(target=worker, daemon=True).start()
        while True:
            event = events.get()
            if event is None:
                break
            yield _sse(event)
        yield _sse({"type": "done"})

    return StreamingResponse(gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


# ---------------------------------------------------------------- 多轮追问(SSE)

class ChatRequest(BaseModel):
    session_id: str
    message: str
    context: dict | None = None    # 首次消息携带:code/stdout/stderr/error/timed_out/diagnosis
    history: list | None = None    # 可选:前端保留的历史消息(会话重建时恢复对话链)


@app.post("/api/chat/stream")
def chat_stream(req: ChatRequest):
    """多轮追问:SSE 实时推送过程事件,最后给本轮结果。

    事件类型:
      session              会话标识
      status               阶段变化(理解问题/生成替代方案/真实运行/验证结果/整理解释/回答)
      delta                自然语言回复的实时增量
      result               本轮结构化结果(kind: text / alt_success / alt_fail)
      error                链路异常
      done                 结束标记
    """
    def gen():
        events: "queue.Queue" = queue.Queue()

        def worker():
            try:
                chat.handle_message(
                    session_id=req.session_id,
                    message=req.message,
                    context=req.context,
                    history=req.history,
                    sink=events.put,
                )
            except Exception as exc:
                events.put({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            finally:
                events.put(None)   # 结束哨兵

        threading.Thread(target=worker, daemon=True).start()
        while True:
            event = events.get()
            if event is None:
                break
            yield _sse(event)
        yield _sse({"type": "done"})

    return StreamingResponse(gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


# ---------------------------------------------------------------- 案例文档库(RAG)

@app.post("/api/kb/upload")
def kb_upload(file: UploadFile = File(...)):
    """上传案例文档(md/txt/docx/pdf):自动切分、向量化、入库。"""
    try:
        data = file.file.read()
        info = rag.ingest(file.filename or "untitled", data)
        return {"ok": True, **info}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/api/kb/docs")
def kb_docs():
    """已入库案例文档列表。"""
    try:
        return {"ok": True, "docs": rag.list_docs(), "stats": rag.stats()}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "docs": [], "stats": {}}


@app.delete("/api/kb/docs/{doc_id}")
def kb_doc_delete(doc_id: str):
    """删除一份案例文档(向量 + 原文留档)。"""
    try:
        return rag.delete_doc(doc_id)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
