#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LLM 封装模块 llm.py（复用阶段 1 核心模块③，供应商可配置）
================================================================
设计要点（对应架构方案 app/llm/ 目录）：
  1. 统一走 OpenAI 兼容协议（/chat/completions），换供应商只改三个环境变量：
     LLM_BASE_URL / LLM_API_KEY / LLM_MODEL（代码不动）；
  2. 支持 SSE 流式输出（stream=True），逐段 yield 文本，前端实现打字机效果；
  3. 异常统一抛 LLMError（带错误码），由上层路由转成中文错误信息。

阶段 2 迁移目标：app/llm/gateway.py（重试/超时/降级）+ providers/
"""

import json
import os

import requests


class LLMError(Exception):
    """LLM 调用异常：携带错误码（LLM_TIMEOUT / LLM_API_ERROR / LLM_NOT_CONFIGURED）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message  # 已是中文，可直接返回给前端


def _get_config():
    """读取环境变量三要素；缺一则抛「未配置」异常。"""
    base_url = os.environ.get("LLM_BASE_URL", "").strip()
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    model = os.environ.get("LLM_MODEL", "").strip()
    if not (base_url and api_key and model):
        raise LLMError("LLM_NOT_CONFIGURED",
                       "未配置大模型服务，请先设置 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 环境变量")
    return base_url, api_key, model


SYSTEM_PROMPT = (
    "你是连锁超市的补货制度助手。请严格依据下方提供的制度条款回答问题，"
    "不要编造条款里没有的内容；若条款不足以回答，请明确说「制度文档中未找到相关规定」。"
    "回答使用简体中文，条理清晰，尽量分点。"
)


def stream_chat(messages, timeout: int = 60):
    """流式调用 LLM：逐段 yield 回答文本片段（供 SSE 转发给前端）。

    参数:
        messages: OpenAI 格式消息列表
                  [{"role": "system"/"user"/"assistant", "content": "..."}]
    yields:
        str —— 模型输出的文本片段（一个或多个字/词）
    异常:
        LLMError —— 超时 / 接口异常 / 未配置
    """
    base_url, api_key, model = _get_config()

    try:
        # OpenAI 兼容协议：加 stream=True 即返回 SSE 流
        resp = requests.post(
            url=f"{base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": messages,
                "temperature": 0.2,   # 制度问答要求低随机性
                "stream": True,       # 流式输出
            },
            stream=True,
            timeout=timeout,
        )
        resp.raise_for_status()

        # 逐行解析 SSE：形如 "data: {...}"，结束标记为 "data: [DONE]"
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            try:
                delta = json.loads(data)["choices"][0].get("delta", {})
            except (json.JSONDecodeError, KeyError, IndexError):
                continue  # 跳过无法解析的心跳/杂项行
            content = delta.get("content")
            if content:
                yield content

    except requests.exceptions.Timeout:
        raise LLMError("LLM_TIMEOUT", "模型响应超时，请稍后重试")
    except requests.exceptions.HTTPError as e:
        # 把供应商返回的 HTTP 错误转成中文提示（如 401 鉴权失败）
        detail = ""
        if e.response is not None:
            try:
                detail = e.response.json().get("error", {}).get("message", "")
            except Exception:
                detail = e.response.text[:200]
        raise LLMError("LLM_API_ERROR", f"模型服务异常（HTTP {e.response.status_code if e.response else '?'}）：{detail}")
    except requests.exceptions.RequestException as e:
        raise LLMError("LLM_API_ERROR", f"模型服务连接异常：{e}")


def build_messages(history, prompt: str, system_prompt: str = None):
    """组装消息列表：系统提示词 + 最近历史 + 本次问题。

    参数:
        history: 最近 6 轮历史 [{"role","content"}, ...]（不含本轮）
        prompt:  拼好检索条款的本次问题
        system_prompt: 系统提示词（不传则用模块默认值）。
                       管理后台可编辑并存库，服务层每次实时读取传入。
    """
    if not system_prompt:
        system_prompt = SYSTEM_PROMPT
    msgs = [{"role": "system", "content": system_prompt}]
    msgs.extend(history)                      # 多轮上下文
    msgs.append({"role": "user", "content": prompt})
    return msgs
