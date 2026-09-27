"""系统提示词：``agents/prompt/*.md``，用 langchain 的 ``PromptTemplate.from_file`` 加载。"""

from pathlib import Path

from langchain_core.prompts import PromptTemplate

__all__ = ["load_system_prompt"]

_PROMPT_DIR = Path(__file__).resolve().parent
# 顺序即拼接顺序：基础人格在前，各挂载点的说明在后
_PARTS = ("system.md", "kb.md")


def load_system_prompt() -> str:
    """拼出完整 system prompt。

    文件走 langchain 的 f-string 模板：**里面不要出现花括号**（会被当成变量解析），
    要表达「某段占位」用 `<...>`（见 ``kb.md`` 的 ``<微服务名>``）。
    """
    return "\n\n".join(
        str(PromptTemplate.from_file(_PROMPT_DIR / name, encoding="utf-8").format())
        for name in _PARTS
    )
