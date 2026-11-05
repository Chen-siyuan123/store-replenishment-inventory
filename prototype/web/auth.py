#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
认证模块 auth.py（管理后台批次 1）
================================================================
- 密码：PBKDF2-HMAC-SHA256（10 万次迭代 + 随机盐），不存明文
- Token：JWT（HS256），有效期 12 小时，payload 携带角色与门店（接口级权限校验依据）
- FastAPI 依赖：
    get_current_user  —— 校验登录态（店长/管理员均放行）
    require_admin     —— 校验管理员角色（后台管理接口专用）
- 签名密钥：优先环境变量 JWT_SECRET；否则生成随机密钥持久化到 secret.key
"""

import os
import secrets
import hashlib
import hmac
from datetime import datetime, timedelta

import jwt
from fastapi import Depends, HTTPException, Request

_DIR = os.path.dirname(os.path.abspath(__file__))
JWT_EXPIRE_HOURS = 12
PBKDF2_ITERATIONS = 100_000


# ============================================================
# 签名密钥
# ============================================================

def _get_secret() -> bytes:
    """获取 JWT 签名密钥：环境变量 > secret.key 文件（自动生成）。"""
    env = os.environ.get("JWT_SECRET", "").strip()
    if env:
        return env.encode()
    key_file = os.path.join(_DIR, "secret.key")
    if os.path.exists(key_file):
        return open(key_file, "rb").read()
    key = secrets.token_hex(32).encode()  # 随机生成并持久化，重启后 token 仍有效
    with open(key_file, "wb") as f:
        f.write(key)
    return key


# ============================================================
# 密码哈希（PBKDF2）
# ============================================================

def hash_password(password: str) -> str:
    """PBKDF2 哈希：格式 pbkdf2$<盐hex>$<摘要hex>。"""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码：同盐重算摘要，恒时比较防时序攻击。"""
    try:
        _, salt_hex, dk_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 bytes.fromhex(salt_hex), PBKDF2_ITERATIONS)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except (ValueError, AttributeError):
        return False


# ============================================================
# JWT 签发与解析
# ============================================================

def create_token(user) -> str:
    """签发 JWT。user 为 sqlite3.Row（users 表行）。"""
    payload = {
        "sub": user["username"],
        "uid": user["id"],
        "role": user["role"],                 # store_manager / admin
        "store_id": user["store_id"],
        "store_name": user["store_name"],
        "exp": datetime.utcnow() + timedelta(hours=JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, _get_secret(), algorithm="HS256")


def decode_token(token: str) -> dict:
    """解析 JWT，过期/签名错误抛 HTTP 401（统一中文错误）。"""
    try:
        return jwt.decode(token, _get_secret(), algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail={"code": "TOKEN_EXPIRED",
                                                     "message": "登录已过期，请重新登录"})
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED",
                                                     "message": "未登录或登录凭证无效"})


# ============================================================
# FastAPI 依赖（接口级权限校验）
# ============================================================

def get_current_user(request: Request) -> dict:
    """登录态依赖：从 Authorization: Bearer <token> 解析用户信息。"""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED",
                                                     "message": "未登录或登录凭证无效"})
    return decode_token(auth_header[len("Bearer "):])


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """管理员依赖：非 admin 角色返回 403。"""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail={"code": "PERMISSION_DENIED",
                                                     "message": "需要总部管理员权限"})
    return user
