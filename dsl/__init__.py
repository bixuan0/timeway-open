"""
scenario DSL 子包：TimeWay 场景语言（.tw）→ ScenarioIR → 现有执行层
====================================================================
分层：
  tokens.py      中英 1:1 关键字映射 + 全角符号归一化
  ir.py          ScenarioIR 数据模型 + 校验器（语言无关，唯一事实源）
  parser.py      .tw 文本（中文/英文）→ ScenarioIR（行式启发式版，向后兼容）
  lark_parser.py .tw 进阶语法（Lark 正式文法版，支持变量/表达式/天气联动/复用）
"""
from .ir import ScenarioIR, validate_ir, fill_v3_defaults
from .parser import parse_tw, parse_file

# lark 为可选依赖：未安装时进阶解析器降级为 None（基础解析/校验不受影响）。
try:
    from .lark_parser import (
        parse_tw_advanced, parse_file_advanced, TwError,
        LARK_GRAMMAR_VERSION,
    )
    HAS_LARK = True
except ImportError:
    HAS_LARK = False
    parse_tw_advanced = None
    parse_file_advanced = None
    TwError = None
    LARK_GRAMMAR_VERSION = None

__all__ = [
    "ScenarioIR", "validate_ir", "fill_v3_defaults",
    "parse_tw", "parse_file",
    "parse_tw_advanced", "parse_file_advanced", "TwError",
    "HAS_LARK", "LARK_GRAMMAR_VERSION",
]
