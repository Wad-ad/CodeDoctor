"""CodeDoctor 后端配置:统一从 backend/.env 读取。"""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# 云端大模型(OpenAI 兼容协议)
API_KEY = os.getenv("CD_API_KEY", "").strip()
BASE_URL = os.getenv("CD_BASE_URL", "https://api.deepseek.com").strip()
MODEL = os.getenv("CD_MODEL", "deepseek-chat").strip()

# 服务
PORT = int(os.getenv("CD_PORT", "8000"))

# 用户代码执行超时(秒):防止死循环把服务卡死
EXEC_TIMEOUT = int(os.getenv("CD_EXEC_TIMEOUT", "10"))

# 知识库
OLLAMA_BASE = os.getenv("CD_OLLAMA_BASE", "http://127.0.0.1:11434").rstrip("/")

# Embedding 后端:ollama=本地模型 / siliconflow=硅基流动云端(OpenAI 兼容,免费)
EMBED_PROVIDER = os.getenv("CD_EMBED_PROVIDER", "ollama").strip().lower()
EMBED_MODEL = os.getenv("CD_EMBED_MODEL", "").strip()  # 留空=按实际生效的后端自动选
EMBED_MODEL_DEFAULTS = {"ollama": "bge-m3", "siliconflow": "BAAI/bge-m3"}
# 硅基流动(选 siliconflow 时必填 Key;免费版模型 ID 不带 Pro/ 前缀)
EMBED_API_KEY = os.getenv("CD_EMBED_API_KEY", "").strip()
EMBED_BASE_URL = os.getenv("CD_EMBED_BASE_URL", "https://api.siliconflow.cn/v1").rstrip("/")
KB_TOP_K = int(os.getenv("CD_KB_TOP_K", "3"))
KB_VECTOR = os.getenv("CD_KB_VECTOR", "1") != "0"          # 置 0 可关闭向量检索(纯 L1)
KB_MAX_DISTANCE = float(os.getenv("CD_KB_MAX_DISTANCE", "0.45"))  # 向量距离上限,超出丢弃

# 案例文档库(RAG):用户上传文档的语义检索参数
CASE_TOP_K = int(os.getenv("CD_CASE_TOP_K", "2"))
CASE_MAX_DISTANCE = float(os.getenv("CD_CASE_MAX_DISTANCE", "0.55"))  # 案例距离上限(内容更杂,略放宽)

# 诊断-修复-验证闭环
MAX_FIX_ROUNDS = int(os.getenv("CD_MAX_FIX_ROUNDS", "3"))

# 多轮追问:替代方案("为什么不那样改")的真实验证重试轮数
CHAT_ALT_ROUNDS = int(os.getenv("CD_CHAT_ALT_ROUNDS", "2"))

# 云端接口结构化输出方式:function_calling / json_schema / json_mode
STRUCTURED_METHOD = os.getenv("CD_STRUCTURED_METHOD", "function_calling").strip()


def is_ollama() -> bool:
    """当前 LLM 配置是否指向本地 Ollama。"""
    return "11434" in BASE_URL or API_KEY == "ollama"


def ollama_base() -> str:
    """Ollama 原生接口地址(去掉 OpenAI 兼容层的 /v1 后缀)。"""
    base = BASE_URL.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base if "11434" in base else OLLAMA_BASE
