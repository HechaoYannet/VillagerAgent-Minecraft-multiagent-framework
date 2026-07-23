"""
共享工具模块

从 model/utils.py 和 pipeline/utils.py 重新导出通用工具函数。
"""

# JSON 提取器 (从 LLM 输出中提取 JSON) — 无外部依赖
from src.utils.serialize import extract_info, find_correct_data  # noqa: F401

# 通用工具 (从 pipeline/utils.py 重导出) — 依赖 colorlog / tiktoken
try:
    from pipeline.utils import (  # noqa: F401
        init_logger,
        format_string,
        smart_truncate,
        extract_information,
        TimeCache,
    )
except ImportError as e:
    # pipeline/utils.py 依赖 colorlog、tiktoken 等可选包
    import logging
    logging.getLogger(__name__).debug(f"pipeline.utils 导入跳过: {e}")
