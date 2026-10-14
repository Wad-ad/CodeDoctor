"""诊断编排入口:把「用户代码 + 真实报错」交给 LangGraph 诊断-验证闭环。

流程:知识库检索 → LLM 结构化诊断 → 修复代码真跑验证 →(未通过则带新报错重试,≤ MAX_FIX_ROUNDS 轮)。
无 API Key 或 AI 调用失败时,自动降级为知识库规则模式。
"""

import graph


def diagnose(code: str, error: dict | None = None, stdout: str = "",
             stderr: str = "", timed_out: bool = False) -> dict:
    """兼容原有调用方式;返回结构化诊断结果(新增 verified / verify_rounds 等字段)。"""
    return graph.run_diagnosis(code=code, error=error, stdout=stdout, stderr=stderr, timed_out=timed_out)
