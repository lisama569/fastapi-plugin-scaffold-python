"""ASGI 入口：``uvicorn main:app``。

默认读取 config.example.toml 并自动发现 plugins_example 包中的插件；
可通过环境变量 ``APP_CONFIG_FILE`` 切换配置文件，
用 ``APP_APP_NAME`` / ``APP_LOG_LEVEL`` / ``APP_PLUGINS`` 等覆盖。
"""

from __future__ import annotations

import os

from app_core.app import create_app

app = create_app(
    config_file=os.environ.get("APP_CONFIG_FILE", "config.example.toml"),
    discover=True,
)
