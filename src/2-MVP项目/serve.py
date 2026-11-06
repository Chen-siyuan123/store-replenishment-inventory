#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
serve.py —— 云端部署启动入口（读取平台注入的 PORT）
================================================================
本地开发仍用 run_local.bat（固定 8765）。托管环境只暴露一个端口，
由平台通过环境变量 PORT 注入，因此需要一个显式读取 PORT 的入口。

    python serve.py            # 监听 0.0.0.0:$PORT（默认 3000）
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)          # 让相对资源（templates / data / chat.db）与本地一致

import uvicorn          # noqa: E402

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "3000"))
    uvicorn.run("server:app", host="0.0.0.0", port=port, log_level="info")
