"""规范化 JSON 与哈希工具：用于步骤输入指纹与完成版冻结哈希。"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical(obj: Any) -> str:
    """键序稳定、无冗余空白的 JSON 文本：同一输入永远得到同一哈希。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()
