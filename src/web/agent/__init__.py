#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能体执行框架（Harness）子包
================================================================
本包只做三件事，刻意保持「薄」：

  harness.py  —— 执行循环：模型决策 → 执行工具 → 结果回填 → 再决策（最多 5 轮）
  store.py    —— 轨迹持久化：agent_runs / agent_steps / agent_actions 三张表
  scenarios.py—— 离线回放：3 条典型轨迹的可执行版本（不消耗模型额度）

设计取向：**不引入 LangGraph / AutoGen 等编排框架**。
原因见 AGENT_HARNESS.md：本场景的循环是「单智能体 + 固定轮次 + 写操作人工确认」，
用 200 行显式控制流比引入一套状态机 DSL 更好调试、更好被业务方审计。
"""
