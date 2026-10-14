# -*- coding: utf-8 -*-
"""让测试能直接 import src 下的模块（与 main.py 里 `from crawler import ...` 的用法一致）。"""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
