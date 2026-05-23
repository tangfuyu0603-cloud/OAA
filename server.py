# ╔══════════════════════════════════════════════════════════════════════╗
# ║   server.py  —  OAA 整合後端（FastAPI + JWT + SSE）                 ║
# ║   高度育成高等學校 · 中央數據庫驗證系統                              ║
# ╚══════════════════════════════════════════════════════════════════════╝
#
# 啟動方式：python server.py
# 登入頁面：http://localhost:8000/login
#
# 依賴：pip install -r requirements.txt

from __future__ import annotations

import asyncio
import json
import os
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from jose import JWTError, jwt
from pydantic import BaseModel

from onecampus_client import (
    AuthError, OneCampusClient, OneCampusError,
    fetch_school_grades, fetch_school_conduct, calc_academic, calc_conduct,
)

# ═══════════════════════════════════════════════════════════════════════
# 設定
# ═══════════════════════════════════════════════════════════════════════

SECRET_KEY = os.getenv("JWT_SECRET", "oaa-dev-secret-請於正式環境替換此值")
ALGORITHM  = "HS256"
TOKEN_TTL  = 86_400  # 24 小時（秒）

BASE_DIR   = Path(__file__).parent

# ── 臺中市校務系統設定（school.tc.edu.tw）─────────────────────────────
# 設定方式：在終端機執行前先設環境變數，或在此直接填入（僅限開發）
#   Windows:  set SCHOOL_CC_SESSION=kgv8aeq4...
#   macOS/Linux: export SCHOOL_CC_SESSION=kgv8aeq4...
SCHOOL_CC_SESSION = os.getenv("SCHOOL_CC_SESSION", "an3dhlvsb7knhfbi28gh446mrlgo486t0rf3hp577pgul5odvioes4a79vsmsju6")
SCHOOL_YEAR       = int(os.getenv("SCHOOL_YEAR",     "114"))
SCHOOL_SEMESTER   = int(os.getenv("SCHOOL_SEMESTER", "2"))

# ── 1campus OAuth 2.0 設定 ─────────────────────────────────────────────
# 申請網址：https://auth.ischool.com.tw/1campus/manage
# 測試帳號：teacher01@1campus.net / 1234（僅限測試 client_id）
OAUTH_CLIENT_ID     = os.getenv("OAUTH_CLIENT_ID",     "90e13e9217481a1721766b9332943bbc")
OAUTH_CLIENT_SECRET = os.getenv("OAUTH_CLIENT_SECRET", "e8566212f3492584087bbb3df63671fe5e1372146e6b438614c4d9ecceba9010")
OAUTH_REDIRECT_URI  = os.getenv("OAUTH_REDIRECT_URI",  "http://localhost:8000/auth/callback")
_OAUTH_AUTH_URL     = "https://auth.ischool.com.tw/oauth/authorize.php"
_OAUTH_TOKEN_URL    = "https://auth.ischool.com.tw/oauth/token.php"
_OAUTH_ME_URL       = "https://auth.ischool.com.tw/services/me.php"

app = FastAPI(
    title="OAA 全面能力指標系統",
    description="高度育成高等學校 學生能力數據 API",
    version="2.0.0",
)


# ═══════════════════════════════════════════════════════════════════════
# 靜態頁面路由
# ═══════════════════════════════════════════════════════════════════════

@app.get("/login", include_in_schema=False)
async def serve_login():
    """提供登入頁面"""
    return FileResponse(BASE_DIR / "oaa-login.html", media_type="text/html")


@app.get("/api/dev-login", include_in_schema=False)
async def dev_login():
    """【開發用】跳過 OAuth，直接以測試帳號進入儀表板。正式上線前請移除此端點。"""
    token = _make_token(
        username="dev@localhost",
        dsns="localhost",
        extra={"name": "開發測試帳號"},
    )
    return RedirectResponse(f"/dashboard?token={token}")


@app.get("/", include_in_schema=False)
@app.get("/dashboard", include_in_schema=False)
async def serve_dashboard():
    """提供儀表板頁面（前端 auth guard 負責未登入時的跳轉）"""
    return FileResponse(BASE_DIR / "OAA_tailwind.html", media_type="text/html")


# ═══════════════════════════════════════════════════════════════════════
# OAuth 2.0 路由
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/oauth/redirect", include_in_schema=False)
async def oauth_redirect():
    """重導向至 1campus 官方登入頁（OAuth 授權碼流程）"""
    params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id":     OAUTH_CLIENT_ID,
        "redirect_uri":  OAUTH_REDIRECT_URI,
        "scope":         "User.Mail,User.BasicInfo",
    })
    return RedirectResponse(f"{_OAUTH_AUTH_URL}?{params}")


@app.get("/auth/callback", include_in_schema=False)
async def oauth_callback(code: str):
    """
    1campus OAuth 回調端點。
    ① 用 code 換 access_token
    ② 取得使用者基本資料（姓名、Email）
    ③ 簽發內部 JWT，帶 token 重導向至儀表板
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        # ① 換 access_token
        token_resp = await client.post(_OAUTH_TOKEN_URL, data={
            "grant_type":    "authorization_code",
            "code":          code,
            "client_id":     OAUTH_CLIENT_ID,
            "client_secret": OAUTH_CLIENT_SECRET,
            "redirect_uri":  OAUTH_REDIRECT_URI,
        })
        if token_resp.status_code != 200:
            raise HTTPException(status_code=502, detail="無法從 1campus 取得 Token，請稍後再試。")
        oauth_token = token_resp.json().get("access_token", "")

        # ② 取得使用者資料
        me_resp = await client.get(_OAUTH_ME_URL, params={"access_token": oauth_token})
        if me_resp.status_code != 200:
            raise HTTPException(status_code=502, detail="無法取得使用者資料，請稍後再試。")
        me = me_resp.json()

    # ③ 整理使用者資訊，簽發內部 JWT
    mail     = me.get("mail", me.get("uuid", "unknown"))
    name     = (me.get("lastName", "") + me.get("firstName", "")).strip() or mail
    jwt_token = _make_token(
        username=mail,
        dsns="auth.ischool.com.tw",
        extra={"name": name, "oauth_token": oauth_token},
    )

    # ④ 帶 token 重導向至儀表板（前端讀取後存入 localStorage 並清除 URL）
    return RedirectResponse(f"/dashboard?token={jwt_token}")


# ═══════════════════════════════════════════════════════════════════════
# Pydantic 模型
# ═══════════════════════════════════════════════════════════════════════

class 登入要求(BaseModel):
    """POST /api/login 請求主體"""
    dsns:     str = "tp001.1campus.net"  # 學校 DSNS 網域
    username: str                         # 學號 / 帳號
    password: str                         # 密碼


# ═══════════════════════════════════════════════════════════════════════
# JWT 工具函式
# ═══════════════════════════════════════════════════════════════════════

def _make_token(username: str, dsns: str, extra: dict | None = None) -> str:
    exp     = datetime.now(timezone.utc) + timedelta(seconds=TOKEN_TTL)
    payload = {"sub": username, "dsns": dsns, "exp": exp}
    if extra:
        payload.update(extra)
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def _decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Token 無效或已過期，請重新登入。")


# ═══════════════════════════════════════════════════════════════════════
# API 路由
# ═══════════════════════════════════════════════════════════════════════

@app.post("/api/login", summary="學生登入・取得 JWT")
async def student_login(req: 登入要求):
    """
    向 1campus 驗證帳密，成功後回傳 JWT（有效期 24 小時）。
    - 帳密錯誤 → HTTP 401
    - 連線異常 → HTTP 400
    """
    try:
        async with OneCampusClient(req.dsns) as client:
            await client.login(req.username, req.password)
    except AuthError:
        raise HTTPException(status_code=401, detail="帳號或密碼錯誤，請確認後重試。")
    except OneCampusError as e:
        raise HTTPException(status_code=400, detail=f"校務系統連線異常：{e}")

    return {"token": _make_token(req.username, req.dsns), "expires_in": TOKEN_TTL}



def _calc_grade(overall: float) -> str:
    if overall >= 90: return "S"
    if overall >= 80: return "A"
    if overall >= 70: return "B"
    if overall >= 60: return "C"
    return "D"


@app.get("/api/sync-all-stream", summary="全員 OAA 同步（SSE）")
async def sync_all_stream(authorization: str = Header(default="")):
    """
    以 Server-Sent Events 串流推送全班 OAA 計算結果。
    需要有效 JWT（Authorization: Bearer <token>）。
    """
    token_str = authorization.removeprefix("Bearer ").strip()
    if not token_str:
        raise HTTPException(status_code=401, detail="請先登入後再執行同步。")
    claims    = _decode_token(token_str)  # 驗證 JWT（過期或無效會拋出 401）
    user_name = claims.get("name", claims.get("sub", "已驗證學生"))

    async def _event_stream():
        # ── 若有真實 session，先推送登入者的真實 OAA 資料 ────────────────
        if SCHOOL_CC_SESSION:
            try:
                subjects, conduct_counts = await asyncio.gather(
                    fetch_school_grades(SCHOOL_CC_SESSION, SCHOOL_YEAR, SCHOOL_SEMESTER),
                    fetch_school_conduct(SCHOOL_CC_SESSION, SCHOOL_YEAR, SCHOOL_SEMESTER),
                )
                ac = calc_academic(subjects)
                ad = calc_conduct(conduct_counts)
                ph = 75.0   # TODO：待串接體育成績 API
                so = 60.0   # TODO：待串接幹部紀錄 API
                overall = round((ac + ph + ad + so) / 4, 1)
                yield f"data: {json.dumps({
                    'type': 'student', 'id': 0, 'name': user_name,
                    'ac': ac, 'ph': ph, 'ad': ad, 'so': so,
                    'overall': overall, 'grade': _calc_grade(overall),
                }, ensure_ascii=False)}\n\n"
            except Exception as exc:
                yield f"data: {json.dumps({'type': 'error', 'message': f'真實資料取得失敗：{exc}'}, ensure_ascii=False)}\n\n"

        yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ═══════════════════════════════════════════════════════════════════════
# 啟動入口
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
