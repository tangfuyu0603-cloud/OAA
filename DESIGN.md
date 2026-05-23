# OAA 整合方案設計文件

高度育成高等學校 · 全面能力指標系統  
設計日期：2026-05-22

---

## 一、決策摘要

| 項目 | 決策 |
|------|------|
| 後端框架 | FastAPI（原生 async） |
| 資料來源 | Mock 資料（`onecampus_client.py`），真實 API 日後填入 |
| Session 持久化 | JWT 存 `localStorage`，24 小時過期（`python-jose`） |
| 架構方案 | 方案 A：單一 `server.py` + 靜態 HTML |

---

## 二、檔案結構

```
OAA/
├── server.py              ← 新增：FastAPI 主程式（API 路由 + 靜態 HTML 服務）
├── onecampus_client.py    ← 不動
├── transformer.py         ← 不動
├── oaa-login.html         ← 新增：獨立登入頁
├── OAA_tailwind.html      ← 修改：auth guard + handleSSEEvent() + 登出按鈕
├── main.py                ← 保留（legacy，不再啟動）
├── OAA.json               ← 不動
└── requirements.txt       ← 新增：pip 依賴清單
```

啟動方式：
```bash
pip install -r requirements.txt
python server.py
# → http://localhost:8000/login
```

---

## 三、API 規格

### `POST /api/login`

**不需要 JWT。**

Request body：
```json
{ "dsns": "tp001.1campus.net", "username": "S1001", "password": "secret" }
```

成功 Response（200）：
```json
{ "token": "eyJ...", "expires_in": 86400 }
```

失敗 Response（401）：
```json
{ "detail": "帳號或密碼錯誤，請確認後重試。" }
```

流程：
1. `OneCampusClient(dsns)` → `await client.login(username, password)`
2. 成功 → `python-jose` 簽發 JWT（`sub: username`、`dsns`、`exp: now+24h`）
3. `AuthError` → HTTP 401

---

### `POST /api/sync-all-stream`

**需要 JWT（`Authorization: Bearer <token>`）。**

SSE 串流，Content-Type: `text/event-stream`

流程：
1. 解碼 JWT → 取得 `username`、`dsns`
2. 重建 `OneCampusClient` 並登入
3. `asyncio.gather` 並發處理 15 位學生
4. 每完成一位立刻推送 SSE 事件

事件格式：
```
data: {"type":"student","id":1,"name":"綾小路 清隆","ac":51.0,"ph":60.0,"ad":37.0,"so":60.0,"overall":49.7,"grade":"C"}

data: {"type":"done"}

data: {"type":"error","message":"連線逾時，請稍後重試。"}
```

---

### 受保護路由

| 路由 | 需要 JWT | 無效時行為 |
|------|---------|-----------|
| `GET /login` | 否 | — |
| `POST /api/login` | 否 | — |
| `GET /` / `GET /dashboard` | 是 | redirect `/login` |
| `POST /api/sync-all-stream` | 是 | HTTP 401 |

---

## 四、前端改動清單

### `oaa-login.html`（新增）

- [ ] `handleLogin()` 發出真實 `POST /api/login`
- [ ] 成功：`localStorage.setItem('token', token)` → `location.href = '/dashboard'`
- [ ] 失敗（401）：終端機欄位輸出 `[ERR] 驗證失敗：帳號或密碼錯誤`，按鈕恢復可用

### `OAA_tailwind.html`（修改三處）

- [ ] **Auth Guard**：頁面載入時，無 token → `location.href = '/login'`
- [ ] **`handleSSEEvent()`**：
  - `type === 'student'`：更新 `STUDENTS` 對應條目的 `ac/ph/ad/so`；若正在檢視該學生則呼叫 `updateDashboard()`
  - `type === 'done'`：關閉同步 Modal，顯示完成訊息
  - `type === 'error'`：Modal 顯示錯誤訊息
- [ ] **登出按鈕**：Header 右上角，點擊後 `localStorage.removeItem('token')` → `location.href = '/login'`

---

## 五、錯誤處理對照表

| 錯誤情境 | 後端回應 | 前端行為 |
|---------|---------|---------|
| 登入欄位空白 | — | 前端直接擋（現有邏輯） |
| 帳密錯誤（`AuthError`） | HTTP 401 | 終端機欄位印 `[ERR] 驗證失敗` |
| JWT 過期 / 無效 | HTTP 401 | 清除 token，跳回 `/login` |
| 網路中斷（sync） | SSE 中斷 | 現有 `showSyncError()` 處理 |
| 1campus 逾時（`FetchTimeoutError`） | SSE 推送 `{type:"error"}` | Modal 顯示錯誤訊息 |

---

## 六、`requirements.txt` 內容

```
fastapi
uvicorn[standard]
httpx
python-jose[cryptography]
```

---

## 七、資料流圖

```
瀏覽器                            server.py (FastAPI)
  │                                      │
  │  POST /api/login                     │
  │  {dsns, username, password} ────────►│
  │                                      │  OneCampusClient.login() [mock]
  │◄── {token, expires_in: 86400} ───────│  python-jose 簽發 JWT
  │  localStorage.setItem('token')       │
  │  location.href = '/dashboard'        │
  │                                      │
  │  GET /dashboard ────────────────────►│
  │◄── OAA_tailwind.html ────────────────│  auth guard: token ✓
  │                                      │
  │  POST /api/sync-all-stream           │
  │  Authorization: Bearer <token> ─────►│  解碼 JWT
  │                                      │  asyncio.gather(15 位學生)
  │◄── data: {type:"student", ...} ──────│  每完成一位即推送
  │       ...（共 15 次）                │
  │◄── data: {type:"done"} ─────────────│
  │  closeSyncModal()                    │
```
