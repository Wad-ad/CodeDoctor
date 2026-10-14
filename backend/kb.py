"""知识库:结构化语料 + 分层检索。

设计:
- 语料 errors.json 是唯一知识来源:AI 模式注入 prompt 作参考,无 Key 模式直接兜底;
- L1 精确检索(主力):错误类型 + 报错消息正则,命中即确定,不引入语义噪声;
- L2 向量检索(兜底):bge-m3 + Chroma 持久化,只在 L1 未命中时启用;
  embedding 后端可切换:ollama(本地,默认)/ siliconflow(硅基流动云端,OpenAI 兼容);
- 向量层不可用时静默降级为纯 L1,不影响核心链路。
"""

import hashlib
import json
import re
from pathlib import Path

import config

_DIR = Path(__file__).resolve().parent / "knowledge"
_CORPUS = _DIR / "errors.json"
_CHROMA_DIR = _DIR / "chroma_db"
_HASH_FILE = _DIR / ".corpus_hash"
_COLLECTION = "codedoctor_kb"

_entries = None
_by_type: dict = {}
_by_id: dict = {}
_vectorstore = None
_vector_state = None  # None=未初始化; True=可用; False=不可用
_vector_error = None  # 向量层初始化失败时的错误摘要(供健康检查) 


# ---------------------------------------------------------------- 语料加载

def load_entries() -> list:
    global _entries, _by_type, _by_id
    if _entries is None:
        data = json.loads(_CORPUS.read_text(encoding="utf-8"))
        _entries = data["entries"]
        _by_type, _by_id = {}, {}
        for e in _entries:
            _by_type.setdefault(e["error_type"], []).append(e)
            _by_id[e["id"]] = e
    return _entries


# ---------------------------------------------------------------- 分层检索

def search(error: dict | None = None, timed_out: bool = False,
           stderr: str = "", code: str = "", top_k: int | None = None) -> list:
    """分层检索入口:先 L1 精确,未命中才走 L2 向量。返回命中的条目列表。"""
    load_entries()
    k = top_k or config.KB_TOP_K

    etype = "__timeout__" if timed_out else (error or {}).get("type")
    message = (error or {}).get("message") or ""

    hits = _l1_search(etype, message)
    if hits:
        return hits[:k]

    if config.KB_VECTOR:
        hits = _l2_search(build_query(error, timed_out, stderr, code), k)
    return hits[:k]


def _l1_search(etype: str | None, message: str) -> list:
    """L1 精确检索:错误类型必须一致;同类型内按消息正则细分场景。

    规则:有具体正则命中的条目优先;都没有命中时才用该类型的兜底条目
    (message_patterns 为空)。类型没有收录则返回空,交给 L2。
    """
    if not etype:
        return []
    same = _by_type.get(etype) or []
    specific, generic = [], []
    for e in same:
        pats = e.get("message_patterns") or []
        if not pats:
            generic.append(e)
        elif any(re.search(p, message, re.IGNORECASE) for p in pats):
            specific.append(e)
    return specific or generic


def build_query(error: dict | None, timed_out: bool, stderr: str, code: str) -> str:
    """构造向量检索的查询文本(errors.json 与案例库共用同一套查询)。"""
    parts = []
    if timed_out:
        parts.append("程序运行超时,停不下来,疑似死循环")
    if error:
        parts.append(f"{error.get('type')}: {error.get('message')}")
        if error.get("snippet"):
            parts.append(f"报错行代码:{error['snippet']}")
    if stderr:
        parts.append(stderr[-500:])
    if not parts and code:
        parts.append(code[-300:])
    return "\n".join(parts)


# ---------------------------------------------------------------- L2 向量层

def _corpus_hash() -> str:
    return hashlib.sha256(_CORPUS.read_bytes()).hexdigest()[:16]


def _active_embed_backend() -> tuple:
    """实际生效的 embedding 后端:(provider, model, base_url)。

    siliconflow 需同时配置 CD_EMBED_PROVIDER=siliconflow 与 CD_EMBED_API_KEY;
    Key 缺失时自动回退 ollama。CD_EMBED_MODEL 留空时,按「实际生效的后端」
    自动选默认模型名(回退场景取 ollama 的 bge-m3,不会误用 BAAI/bge-m3)。
    """
    if config.EMBED_PROVIDER == "siliconflow" and config.EMBED_API_KEY:
        provider, base_url = "siliconflow", config.EMBED_BASE_URL
    else:
        provider, base_url = "ollama", config.OLLAMA_BASE
    model = config.EMBED_MODEL or config.EMBED_MODEL_DEFAULTS.get(provider, "bge-m3")
    return provider, model, base_url


def _make_embeddings():
    """构造 embedding 实例,返回 (embeddings, fingerprint)。

    fingerprint 记录「实际使用的后端 + 模型」,与语料哈希共同决定索引是否重建——
    换后端时自动重建,防止不同后端生成的向量混用导致检索质量劣化。
    """
    provider, model, base_url = _active_embed_backend()
    if provider == "siliconflow":
        from langchain_openai import OpenAIEmbeddings

        emb = OpenAIEmbeddings(
            model=model,
            api_key=config.EMBED_API_KEY,
            base_url=base_url,
            # 关键:关闭 tiktoken 预处理,直接发送原始文本。
            # 默认开启时 LangChain 会把文本编码成 OpenAI 专有的 token 数组,
            # 第三方兼容服务(硅基流动)不认这种输入格式。
            check_embedding_ctx_length=False,
        )
        return emb, f"siliconflow:{model}"

    from langchain_ollama import OllamaEmbeddings

    emb = OllamaEmbeddings(model=model, base_url=base_url)
    return emb, f"ollama:{model}"


def _entry_embed_text(e: dict) -> str:
    parts = [e.get("title", ""), e.get("error_type", "")]
    parts += e.get("scenarios") or []
    parts.append(e.get("explain", ""))
    parts += e.get("fix_points") or []
    return "。".join(p for p in parts if p)


def _get_vectorstore():
    global _vectorstore, _vector_state, _vector_error
    if _vector_state is not None:
        return _vectorstore
    try:
        from langchain_chroma import Chroma

        embeddings, fp = _make_embeddings()
        # 指纹 = 语料哈希 + 后端标识:任一变化都触发索引重建
        current = f"{_corpus_hash()}|{fp}"
        stored = _HASH_FILE.read_text(encoding="utf-8").strip() if _HASH_FILE.exists() else ""

        store = Chroma(
            collection_name=_COLLECTION,
            embedding_function=embeddings,
            persist_directory=str(_CHROMA_DIR),
            collection_metadata={"hnsw:space": "cosine"},
        )

        try:
            count = store._collection.count()
        except Exception:
            count = 0

        if stored != current or count == 0:
            # 语料或 embedding 后端变了:清空集合后整体重建。
            # 用 Chroma 自身的 reset(不碰文件系统),避免被本机 safe-delete 防护拦截。
            try:
                store.reset_collection()
            except Exception:
                try:
                    store.delete_collection()
                except Exception:
                    pass
                store = Chroma(
                    collection_name=_COLLECTION,
                    embedding_function=embeddings,
                    persist_directory=str(_CHROMA_DIR),
                    collection_metadata={"hnsw:space": "cosine"},
                )
            entries = load_entries()
            store.add_texts(
                texts=[_entry_embed_text(e) for e in entries],
                ids=[e["id"] for e in entries],
                metadatas=[{"entry_id": e["id"], "error_type": e["error_type"]} for e in entries],
            )
            _HASH_FILE.write_text(current, encoding="utf-8")

        _vectorstore = store
        _vector_state = True
    except Exception as exc:
        _vector_state = False
        _vectorstore = None
        _vector_error = f"{type(exc).__name__}: {exc}"
    return _vectorstore


def _l2_search(query: str, k: int) -> list:
    """L2 向量检索:语义相似 top-k;距离阈值外的结果丢弃,宁缺毋滥。"""
    store = _get_vectorstore()
    if store is None or not query.strip():
        return []
    try:
        results = store.similarity_search_with_score(query, k=k)
    except Exception:
        return []
    hits = []
    for doc, distance in results:
        if distance > config.KB_MAX_DISTANCE:
            continue
        eid = (doc.metadata or {}).get("entry_id") or doc.id
        e = _by_id.get(eid)
        if e:
            hits.append(e)
    return hits


# ---------------------------------------------------------------- 供上层使用

def backend_status() -> dict:
    """向量后端状态(供健康检查/调试):配置、实际生效的后端与错误信息。"""
    provider, model, base_url = _active_embed_backend()
    return {
        "provider": provider,        # 实际生效:ollama / siliconflow
        "model": model,
        "base_url": base_url,
        "vector_ready": _vector_state,  # None=未初始化; True=可用; False=失败(纯 L1)
        "vector_error": _vector_error,
    }


def format_entries(entries: list) -> str:
    """把知识库条目格式化成可注入 prompt 的「参考知识」文本。"""
    if not entries:
        return ""
    blocks = []
    for e in entries:
        lines = [f"### {e['title']}({e['error_type']})", e.get("explain", "")]
        if e.get("fix_points"):
            lines.append("修复要点:" + ";".join(e["fix_points"]))
        if e.get("example_bad"):
            lines.append("错误示例:" + e["example_bad"])
        if e.get("example_good"):
            lines.append("正确示例:" + e["example_good"])
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def rule_diagnosis(error: dict | None, timed_out: bool, stderr: str = "") -> dict:
    """无 AI(或调用失败)时的兜底诊断:直接用知识库条目拼装。"""
    hits = search(error, timed_out=timed_out, stderr=stderr, top_k=1)

    notice = ""
    if not config.API_KEY:
        notice = ("当前未配置 API Key,以上为内置知识库的基础提示。"
                  "在 backend/.env 中填入 Key 后,可获得针对你代码的完整 AI 诊断与一键修复。")

    if hits:
        e = hits[0]
        fix_points = list(e.get("fix_points") or [])
        if error and error.get("line"):
            fix_points.insert(0, f"报错定位:第 {error['line']} 行 —— {error.get('snippet', '')}")
        scenarios = e.get("scenarios") or []
        return {
            "explain": e.get("explain", ""),
            "reason": ("常见场景:" + "、".join(scenarios)) if scenarios else "",
            "fix_points": fix_points,
            "error_line": (error or {}).get("line"),
            "fixed_code": "",
            "source": "rule",
            "knowledge_ids": [e["id"]],
            "notice": notice,
        }

    if timed_out:
        explain = "程序运行了很久都没有结束,看起来是「死循环」——某段代码在反复执行,却永远停不下来。"
    elif error:
        explain = f"程序抛出了 {error.get('type')} 异常。"
    else:
        explain = "程序运行失败,但没有捕捉到标准的报错信息。"
    return {
        "explain": explain,
        "reason": (error or {}).get("message") or "",
        "fix_points": [],
        "error_line": (error or {}).get("line"),
        "fixed_code": "",
        "source": "rule",
        "knowledge_ids": [],
        "notice": notice,
    }
