"""案例文档库(RAG):文档加载 → 智能切分 → 向量化 → 入库 → 语义检索。

管道基于 LangChain 标准组件:
- 加载:TextLoader(md/txt) / Docx2txtLoader(docx) / PyPDFLoader(pdf);
- 切分:MarkdownHeaderTextSplitter(按标题切)+ RecursiveCharacterTextSplitter(超长细分),
  前置「代码块保护」薄层 —— 避免代码块被劈开、代码注释里的 # 被误判为标题;
- 向量化:复用 kb.py 的 embedding 工厂(ollama / siliconflow 可切换);
- 入库:Chroma 独立集合 codedoctor_cases,与 errors.json 的 codedoctor_kb 分区管理;
- 检索:诊断时按报错语义召回 top-k 片段,注入 prompt 作「参考案例」;
- 原文留档 knowledge/uploads/,换 embedding 后端时自动重建全部索引。
"""

import hashlib
import re
from datetime import datetime
from pathlib import Path

import config
import kb

_DIR = Path(__file__).resolve().parent / "knowledge"
_UPLOADS = _DIR / "uploads"           # 原文留档(重建索引的依据)
_CHROMA_DIR = _DIR / "chroma_db"
_FP_FILE = _DIR / ".cases_fingerprint"
_COLLECTION = "codedoctor_cases"

SUPPORTED_EXTS = {".md", ".markdown", ".txt", ".docx", ".pdf"}
MAX_FILE_MB = 10

CHUNK_SIZE = 700
CHUNK_OVERLAP = 80
MIN_CHUNK_CHARS = 60

_store = None
_state = None   # None=未初始化; True=可用; False=失败
_error = None   # 初始化失败时的错误摘要(供健康检查)

_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)
_PLACEHOLDER = re.compile(r"\[\[CB(\d+)\]\]")
_CB_FRAGMENT = re.compile(r"\[\[CB\d{0,4}\]{0,2}")   # 被切分破坏的占位符碎片
_MD_HEADING = re.compile(r"^#{1,3}\s", re.MULTILINE)


# ---------------------------------------------------------------- 加载层

def _load_document(path: Path, filename: str) -> list:
    """按扩展名选择 LangChain 文档加载器,统一 source 为原始文件名。"""
    ext = path.suffix.lower()
    if ext in (".md", ".markdown", ".txt"):
        from langchain_community.document_loaders import TextLoader

        loader = TextLoader(str(path), encoding="utf-8", autodetect_encoding=True)
    elif ext == ".docx":
        from langchain_community.document_loaders import Docx2txtLoader

        loader = Docx2txtLoader(str(path))
    elif ext == ".pdf":
        from langchain_community.document_loaders import PyPDFLoader

        loader = PyPDFLoader(str(path))
    else:
        raise ValueError(f"不支持的文档格式:{ext}")

    docs = loader.load()
    for d in docs:
        d.metadata["source"] = filename
    return docs


# ---------------------------------------------------------------- 切分层

def _md_splitter():
    from langchain_text_splitters import MarkdownHeaderTextSplitter

    return MarkdownHeaderTextSplitter(
        headers_to_split_on=[("#", "h1"), ("##", "h2"), ("###", "h3")],
        strip_headers=False,   # 标题保留在块内,检索与注入时需要标题上下文
    )


def _rec_splitter():
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    return RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""],   # 中文友好
    )


def _protect_code_blocks(text: str) -> tuple:
    """切分前把代码块替换为占位符,防止:①代码块被劈开 ②注释里的 # 被当标题切。"""
    blocks = []

    def repl(m):
        blocks.append(m.group(0))
        return f"\n\n[[CB{len(blocks) - 1}]]\n\n"

    return _CODE_FENCE.sub(repl, text), blocks


def _restore_code_blocks(text: str, blocks: list) -> str:
    """还原代码块;顺带清理(概率极低的)占位符碎片,保证输出永远是干净文本。"""
    def repl(m):
        idx = int(m.group(1))
        return blocks[idx] if idx < len(blocks) else ""

    text = _PLACEHOLDER.sub(repl, text)
    return _CB_FRAGMENT.sub("", text)


def _split_documents(docs: list) -> list:
    """实测版切分:Markdown 标题优先 → 超长递归细分 → 还原代码块 → 合并碎块。"""
    md_splitter = rec_splitter = None
    chunks = []
    for doc in docs:
        text = (doc.page_content or "").strip()
        if not text:
            continue
        protected, blocks = _protect_code_blocks(text)

        if _MD_HEADING.search(protected):
            if md_splitter is None:
                md_splitter = _md_splitter()
            parts = md_splitter.split_text(protected)
        else:
            from langchain_core.documents import Document

            parts = [Document(page_content=protected, metadata=dict(doc.metadata))]

        for part in parts:
            subs = [part]
            if len(part.page_content) > CHUNK_SIZE:
                if rec_splitter is None:
                    rec_splitter = _rec_splitter()
                subs = rec_splitter.split_documents([part])
            for s in subs:
                s.page_content = _restore_code_blocks(s.page_content, blocks).strip()
                if s.page_content:
                    chunks.append(s)
    return _merge_small(chunks)


def _merge_small(chunks: list) -> list:
    """过短的碎块并入前一块,避免语义碎片。"""
    merged = []
    for c in chunks:
        text = (c.page_content or "").strip()
        if not text:
            continue
        if merged and len(text) < MIN_CHUNK_CHARS:
            prev = merged[-1]
            prev.page_content = f"{prev.page_content}\n\n{text}"
        else:
            merged.append(c)
    return merged


# ---------------------------------------------------------------- 向量层

def _fingerprint() -> str:
    st = kb.backend_status()
    return f"cases|{st['provider']}:{st['model']}"


def _new_store():
    from langchain_chroma import Chroma

    embeddings, _ = kb._make_embeddings()
    return Chroma(
        collection_name=_COLLECTION,
        embedding_function=embeddings,
        persist_directory=str(_CHROMA_DIR),
        collection_metadata={"hnsw:space": "cosine"},
    )


def _get_store():
    global _store, _state, _error
    if _state is not None:
        return _store
    try:
        fp = _fingerprint()
        store = _new_store()
        stored = _FP_FILE.read_text(encoding="utf-8").strip() if _FP_FILE.exists() else ""
        if stored != fp:
            # 首次使用或换过 embedding 后端:从 uploads/ 原文全量重建
            store = _rebuild_all(store, fp)
        _store = store
        _state = True
    except Exception as exc:
        _state = False
        _store = None
        _error = f"{type(exc).__name__}: {exc}"
    return _store


def _rebuild_all(store, fp: str):
    """全量重建索引:清空集合后按 uploads/ 里的原文逐份重放。"""
    try:
        store.reset_collection()
    except Exception:
        store = _new_store()
    files = sorted(_UPLOADS.glob("*")) if _UPLOADS.exists() else []
    for f in files:
        if not f.is_file() or f.name.startswith("."):
            continue
        doc_id, original = _parse_stored_name(f.name)
        try:
            _index_file(store, f, original, doc_id)
        except Exception:
            continue   # 单份文件坏了不阻塞其他文档
    _FP_FILE.write_text(fp, encoding="utf-8")
    return store


def _parse_stored_name(name: str) -> tuple:
    if "__" in name:
        doc_id, original = name.split("__", 1)
        return doc_id, original
    return name[:12], name


def _chunk_meta(doc_id: str, filename: str, index: int, chunk, uploaded_at: str) -> dict:
    md = chunk.metadata or {}
    title = md.get("h3") or md.get("h2") or md.get("h1") or ""
    return {
        "doc_id": doc_id,
        "filename": filename,
        "chunk_index": int(index),
        "title": str(title)[:120],
        "uploaded_at": uploaded_at,
    }


def _index_file(store, path: Path, original: str, doc_id: str, uploaded_at: str = "") -> int:
    """加载 + 切分 + 入库,返回 chunk 数。"""
    docs = _load_document(path, original)
    chunks = _split_documents(docs)
    if not chunks:
        raise ValueError("文档里没有可索引的内容")
    uploaded_at = uploaded_at or datetime.now().strftime("%Y-%m-%d %H:%M")
    store.add_texts(
        texts=[c.page_content for c in chunks],
        ids=[f"{doc_id}-{i}" for i in range(len(chunks))],
        metadatas=[_chunk_meta(doc_id, original, i, c, uploaded_at) for i, c in enumerate(chunks)],
    )
    return len(chunks)


def _delete_chunks(store, doc_id: str) -> None:
    try:
        store._collection.delete(where={"doc_id": doc_id})
    except Exception:
        pass


# ---------------------------------------------------------------- 对外接口

def ingest(filename: str, data: bytes) -> dict:
    """上传入库:校验 → 存原文 → 加载切分 → 向量化入库(同内容自动覆盖)。"""
    name = Path(filename or "untitled").name or "untitled"
    ext = Path(name).suffix.lower()
    if ext not in SUPPORTED_EXTS:
        raise ValueError(f"不支持的格式 {ext}。支持:{'、'.join(sorted(SUPPORTED_EXTS))}")
    if not data:
        raise ValueError("文件是空的")
    if len(data) > MAX_FILE_MB * 1024 * 1024:
        raise ValueError(f"文件超过 {MAX_FILE_MB}MB 上限")

    store = _get_store()
    if store is None:
        raise RuntimeError(f"向量服务不可用:{_error or '未知原因'}")

    doc_id = hashlib.sha256(data).hexdigest()[:12]
    _UPLOADS.mkdir(parents=True, exist_ok=True)
    stored_path = _UPLOADS / f"{doc_id}__{name}"
    stored_path.write_bytes(data)

    _delete_chunks(store, doc_id)   # 同一文档重传:先清旧,再重新索引
    count = _index_file(store, stored_path, name, doc_id)

    return {"doc_id": doc_id, "filename": name, "chunks": count}


def search_cases(query: str, k: int | None = None) -> list:
    """语义检索案例片段(诊断时注入 prompt 用)。空库/失败时返回空列表,不影响主链路。"""
    store = _get_store()
    if store is None or not (query or "").strip():
        return []
    try:
        if store._collection.count() == 0:
            return []
        results = store.similarity_search_with_score(query, k=k or config.CASE_TOP_K)
    except Exception:
        return []
    hits = []
    for doc, distance in results:
        if distance > config.CASE_MAX_DISTANCE:
            continue
        md = doc.metadata or {}
        hits.append({
            "text": doc.page_content,
            "filename": md.get("filename", ""),
            "title": md.get("title", ""),
            "distance": round(float(distance), 3),
        })
    return hits


def format_cases(cases: list) -> str:
    """把案例检索结果格式化成可注入 prompt 的「参考案例」文本。"""
    if not cases:
        return ""
    blocks = []
    for c in cases:
        label = f"### 案例片段(来源:{c.get('filename') or '未知文档'}"
        if c.get("title"):
            label += f" · {c['title']}"
        label += ")"
        blocks.append(f"{label}\n{c.get('text', '')}")
    return "\n\n".join(blocks)


def list_docs() -> list:
    """已入库文档列表(按上传时间倒序)。直接从 Chroma metadata 聚合,单一数据源。"""
    store = _get_store()
    if store is None:
        return []
    try:
        data = store._collection.get(include=["metadatas"], limit=100000)
    except Exception:
        return []
    agg = {}
    for meta in data.get("metadatas") or []:
        if not meta:
            continue
        did = meta.get("doc_id")
        if not did:
            continue
        info = agg.get(did)
        if info is None:
            info = agg[did] = {
                "doc_id": did,
                "filename": meta.get("filename", ""),
                "chunks": 0,
                "uploaded_at": meta.get("uploaded_at", ""),
            }
        info["chunks"] += 1
    return sorted(agg.values(), key=lambda x: x.get("uploaded_at", ""), reverse=True)


def delete_doc(doc_id: str) -> dict:
    """删除一份文档:清它的全部向量 + 移除原文留档。"""
    store = _get_store()
    if store is None:
        raise RuntimeError(f"向量服务不可用:{_error or '未知原因'}")
    try:
        store._collection.delete(where={"doc_id": doc_id})
    except Exception as exc:
        raise RuntimeError(f"删除索引失败:{exc}")
    removed = 0
    if _UPLOADS.exists():
        for f in _UPLOADS.glob(f"{doc_id}__*"):
            try:
                f.unlink()
                removed += 1
            except Exception:
                pass
    return {"ok": True, "doc_id": doc_id, "files_removed": removed}


def stats() -> dict:
    """案例库概况(健康检查用)。"""
    store = _get_store()
    if store is None:
        return {"docs": 0, "chunks": 0, "ready": False, "error": _error}
    docs = list_docs()
    return {
        "docs": len(docs),
        "chunks": sum(d.get("chunks", 0) for d in docs),
        "ready": True,
        "error": None,
    }
