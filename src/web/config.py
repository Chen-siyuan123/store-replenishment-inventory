#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配置加载与自检 config.py
================================================================
把「模型配置」从「手敲四条 set 命令」变成「一份 .env + 一条启动命令」。

为什么必须单独抽一个模块、而且要被最先 import：
  1. kbstore.py 的检索门限是**模块级常量**（import 的那一刻就读环境变量），
     如果 .env 加载晚于 `import kbstore`，门限就不会生效；
  2. 配置错误（比如把对话模型名填进向量模型位）在过去会以 requests 的
     400 原文抛出来，运维看了一头雾水；这里统一转成中文诊断。

使用约定：
  server.py 里第一个本地 import 必须是 `import config`。
  单独跑脚本时（如 eval/probe_retrieval.py），import config 也能自动补加载。

安全约定：
  .env 存真实密钥，必须写进 .gitignore，绝不能提交到 public 仓库。
  .env.example 只放占位符，供别人复制。
"""

import os

WEB_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(WEB_DIR, ".env")

# 必填三项：缺任何一项，问答主链路直接不可用
REQUIRED_KEYS = ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")
# 向量模型：缺失只影响语义检索（自动降级为纯关键词），问答仍可用
EMBED_KEY = "LLM_EMBED_MODEL"

# 智谱已知的模型命名特征，用于识别「填错位置」
_CHAT_HINTS = ("glm-", "gpt-", "qwen", "deepseek", "claude")
_EMBED_HINTS = ("embedding", "embed", "bge", "text-embedding")

_loaded_path = None    # 实际加载的 .env 路径（None 表示没找到）


# ============================================================
# 一、.env 加载
# ============================================================

def _parse_env_line(raw: str):
    """解析一行 KEY=VALUE。支持 # 注释、export 前缀、成对引号。解析不出返回 None。"""
    line = raw.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[7:].strip()
    if "=" not in line:
        return None
    key, val = line.split("=", 1)
    key, val = key.strip(), val.strip()
    if not key:
        return None
    # 去掉成对引号（允许 KEY="a b" 这类含空格的值）
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
        val = val[1:-1]
    return key, val


def load_env(path: str = None, override: bool = False) -> int:
    """把 .env 读入 os.environ，返回实际写入的键数。

    override=False（默认）：已存在的真实环境变量优先，.env 只做兜底。
    这样在 CI/服务器上可以用真正的环境变量覆盖本地文件，不必改文件。

    刻意不依赖 python-dotenv：文件格式很简单，自己解析 20 行就够，
    少一个依赖就少一类「装不上环境」的故障。
    """
    global _loaded_path
    path = path or ENV_PATH
    if not os.path.exists(path):
        _loaded_path = None
        return 0

    written = 0
    with open(path, "r", encoding="utf-8-sig") as f:   # utf-8-sig 兼容记事本存的 BOM
        for raw in f:
            parsed = _parse_env_line(raw)
            if not parsed:
                continue
            key, val = parsed
            if override or not os.environ.get(key):
                os.environ[key] = val
                written += 1
    _loaded_path = path
    return written


def get(key: str, default: str = "") -> str:
    """读配置并去空白（避免 .env 末尾多一个空格就判空）。"""
    return (os.environ.get(key) or default).strip()


def mask(secret: str) -> str:
    """密钥脱敏，只留首尾各 4 位，供日志/诊断打印。"""
    if not secret:
        return "(空)"
    if len(secret) <= 10:
        return "*" * len(secret)
    return f"{secret[:4]}{'*' * 8}{secret[-4:]}"


# ============================================================
# 二、配置自检（启动时打印，把配置错误挡在第一个请求之前）
# ============================================================

def diagnose():
    """检查配置合理性，返回 [(level, message)]，level ∈ ok / warn / error。

    只做**静态**判断（不联网），启动瞬间就能跑完。
    真正的连通性由 selftest.py 负责。
    """
    items = []

    if _loaded_path:
        items.append(("ok", f"已加载配置文件 {_loaded_path}"))
    else:
        items.append(("warn", f"未找到 {ENV_PATH}，仅使用系统环境变量；"
                              f"可复制 .env.example 为 .env 后填写"))

    base_url = get("LLM_BASE_URL")
    api_key = get("LLM_API_KEY")
    model = get("LLM_MODEL")
    embed_model = get(EMBED_KEY)

    # ---- 必填三项 ----
    missing = [k for k, v in zip(REQUIRED_KEYS, (base_url, api_key, model)) if not v]
    if missing:
        items.append(("error", f"缺少必填配置：{'、'.join(missing)}；"
                               f"问答功能将不可用（接口会返回 LLM_NOT_CONFIGURED）"))
    if base_url:
        if not base_url.startswith(("http://", "https://")):
            items.append(("error", f"LLM_BASE_URL 必须以 http:// 或 https:// 开头，当前为 {base_url!r}"))
        elif not base_url.rstrip("/").endswith("/v4"):
            items.append(("warn", f"LLM_BASE_URL={base_url} 不像智谱地址，"
                                  f"智谱应为 https://open.bigmodel.cn/api/paas/v4"))
        else:
            items.append(("ok", f"服务地址：{base_url}"))
    if api_key:
        items.append(("ok", f"LLM_API_KEY 已配置（{mask(api_key)}）"))
        # 智谱的 key 形如 <id>.<secret>；OpenAI 的形如 sk-xxx，都允许，只提示不拦
        if "." not in api_key and not api_key.startswith("sk-"):
            items.append(("warn", "LLM_API_KEY 格式既不是智谱的 id.secret，也不是 sk- 开头，请确认"))
    if model:
        low = model.lower()
        if any(h in low for h in _EMBED_HINTS):
            items.append(("error", f"LLM_MODEL={model} 看起来是向量模型，"
                                   f"它不能用于对话；对话应填 glm-4-flash 之类的模型"))
        else:
            items.append(("ok", f"对话模型：{model}"))

    # ---- 向量模型（缺失只降级，不阻断）----
    if not embed_model:
        items.append(("warn", f"未配置 {EMBED_KEY}，向量检索会降级为哈希伪向量（仅关键词有效）。"
                              f"智谱请填 embedding-3"))
    else:
        low = embed_model.lower()
        if any(h in low for h in _CHAT_HINTS):
            # 这就是之前实测会 400 的那类错误：用对话模型名调 /embeddings
            items.append(("error", f"{EMBED_KEY}={embed_model} 是对话模型，"
                                   f"调 /embeddings 会返回 400（智谱错误码 1213）；"
                                   f"应改为 embedding-3"))
        elif not any(h in low for h in _EMBED_HINTS):
            items.append(("warn", f"{EMBED_KEY}={embed_model} 名称不像向量模型，请确认"))
        else:
            items.append(("ok", f"向量模型：{embed_model}"))
        if model and embed_model == model:
            items.append(("error", f"LLM_MODEL 与 {EMBED_KEY} 相同（都是 {model}），"
                                   f"对话与向量不能共用一个模型"))

    return items


def has_blocking_error(items=None) -> bool:
    """诊断结果里是否存在 error 级问题（warn 不算）。"""
    items = items if items is not None else diagnose()
    return any(lv == "error" for lv, _ in items)


def print_diagnose(stream=None) -> bool:
    """打印诊断结果，返回「是否可以正常提供问答」。"""
    import sys
    stream = stream or sys.stdout
    items = diagnose()
    icon = {"ok": "[OK]  ", "warn": "[WARN]", "error": "[ERR] "}
    stream.write("\n================ 模型配置自检 ================\n")
    for level, msg in items:
        stream.write(f"{icon.get(level, '      ')} {msg}\n")
    blocking = has_blocking_error(items)
    stream.write("==============================================\n")
    if blocking:
        stream.write("结论：配置有错误，问答不可用。请修改 .env 后重启（参考 .env.example）。\n\n")
    else:
        stream.write("结论：配置可正常使用。\n\n")
    stream.flush()
    return not blocking


# 被 import 时立即加载：保证 kbstore / llm 等模块级读取能拿到值
try:
    load_env()
except Exception:      # .env 格式异常不能阻断服务启动
    pass


if __name__ == "__main__":
    # 单独运行即可做配置体检：python config.py
    import sys
    sys.exit(0 if print_diagnose() else 1)
