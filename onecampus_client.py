# ╔══════════════════════════════════════════════════════════════════════╗
# ║   onecampus_client.py  —  1campus 非同步 HTTP 爬蟲模組              ║
# ║   嚴禁使用 Puppeteer / Selenium，純 HTTPX AsyncClient 實作          ║
# ╚══════════════════════════════════════════════════════════════════════╝
#
# 依賴：pip install httpx
#
# 使用方式（Context Manager，確保連線池正確釋放）：
#
#   async with OneCampusClient(dsns_domain="tp123.1campus.net") as client:
#       await client.login("帳號", "密碼")
#       data = await client.fetch_all_data()
#       print(data.profile)
#
# ══════════════════════════════════════════════════════════════════════

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# 自訂例外體系
# ═══════════════════════════════════════════════════════════════════════

class OneCampusError(Exception):
    """1campus 客戶端基底例外，所有錯誤皆繼承此類別"""

class AuthError(OneCampusError):
    """
    驗證失敗。
    觸發時機：HTTP 401、帳號密碼錯誤、Token 過期。
    處理建議：清除快取 Token 並要求使用者重新登入。
    """

class RateLimitError(OneCampusError):
    """
    請求頻率超限（HTTP 429）。
    觸發時機：超過最大重試次數後仍被限流。
    處理建議：等待較長時間後再次嘗試，或降低並發請求數量。
    """

class FetchTimeoutError(OneCampusError):
    """
    請求逾時。
    觸發時機：網路不穩、校務系統回應過慢。
    處理建議：提示使用者確認網路狀態，再重新同步。
    """

class NotLoggedInError(OneCampusError):
    """
    尚未登入。
    觸發時機：未呼叫 login() 就直接呼叫 fetch_all_data()。
    """


# ═══════════════════════════════════════════════════════════════════════
# 回傳資料容器
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class StudentAllData:
    """
    fetch_all_data() 的整合回傳結果。
    各欄位的鍵名與結構取決於 1campus 的實際 API 回應，
    正式串接後請依 normalize_1campus_to_oaa()（見 main.py）進行轉換。
    """
    profile:   dict = field(default_factory=dict)  # 基本資料（姓名、班級、座號…）
    grades:    dict = field(default_factory=dict)  # 各科成績（平均分、各科分數…）
    conduct:   dict = field(default_factory=dict)  # 獎懲出缺席（缺席天數、嘉獎數…）
    positions: dict = field(default_factory=dict)  # 幹部資訊（班級幹部、社團職務…）


# ═══════════════════════════════════════════════════════════════════════
# OneCampusClient 主類別
# ═══════════════════════════════════════════════════════════════════════

class OneCampusClient:
    """
    1campus 非同步 HTTP 客戶端。

    ┌──────────────────────────────────────────────────────────────────┐
    │  關於 DSNS（Distributed School Network System）                  │
    │                                                                  │
    │  1campus 採用 DSNS 架構：每所學校擁有唯一識別碼（dsns），        │
    │  系統透過此識別碼將請求路由到各校專屬的後端伺服器。              │
    │                                                                  │
    │  如何取得學校的 dsns_domain：                                    │
    │  1. 開啟學校的 1campus 登入頁                                    │
    │  2. F12 → Network → 觀察登入請求的 Request URL Host 欄位        │
    │  3. 通常格式為 "學校代碼.1campus.net"                           │
    │     例：tp001.1campus.net、ntpc123.1campus.net                   │
    └──────────────────────────────────────────────────────────────────┘
    """

    # ── DSNS 中央路由伺服器（查詢學校 API Base URL 用）
    # ★ 若 1campus 不需要 DSNS 查詢、直接使用固定網域，可將此常數設為 None
    _DSNS_LOOKUP_URL = "https://dsns.1campus.net/school/1/lookup"

    def __init__(
        self,
        dsns_domain: str,
        *,
        login_timeout: float = 15.0,
        fetch_timeout: float = 20.0,
        max_retries: int = 3,
    ) -> None:
        """
        初始化客戶端（此時尚未建立 HTTP 連線，需透過 async with 啟動）。

        參數：
            dsns_domain    — 學校的 1campus DSNS 網域，例如 "tp001.1campus.net"
                             從 Chrome DevTools → Network → 登入請求的 Host 欄位取得
            login_timeout  — 登入請求逾時秒數（預設 15 秒）
            fetch_timeout  — 資料撈取請求逾時秒數（預設 20 秒）
            max_retries    — 遭遇 429 或 Timeout 時的最大重試次數（預設 3 次）
        """
        self.dsns_domain   = dsns_domain.strip().rstrip("/")
        self.login_timeout = login_timeout
        self.fetch_timeout = fetch_timeout
        self.max_retries   = max_retries

        # 登入後快取的憑證（二擇一，依 1campus 使用 JWT 或 Session Cookie）
        self._access_token:    str | None = None  # JWT Bearer Token
        self._session_cookie:  str | None = None  # Session-based Cookie

        # 學校 API 基底 URL（DSNS 查詢後填入）
        self._api_base: str | None = None

        # HTTPX AsyncClient（在 __aenter__ 初始化，__aexit__ 關閉）
        self._client: httpx.AsyncClient | None = None

    # ── Context Manager ────────────────────────────────────────────────

    async def __aenter__(self) -> "OneCampusClient":
        """建立 HTTPX AsyncClient（連線池在整個 Session 中復用）"""
        self._client = httpx.AsyncClient(
            # ★ 若伺服器支援 HTTP/2，取消下行註解可降低多路請求的延遲
            # http2=True,
            headers={
                # ★ 從 DevTools Request Headers 複製瀏覽器 User-Agent，
                #   避免被伺服器的 Bot 偵測機制封鎖
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept":          "application/json",
                "Accept-Language": "zh-TW,zh;q=0.9",
            },
            limits=httpx.Limits(
                max_connections=10,        # 最大同時連線數
                max_keepalive_connections=5,
            ),
            follow_redirects=True,
            # ★ 測試環境如遇 SSL 憑證問題，可暫時設為 verify=False，
            #   正式環境務必保持 True
            # verify=False,
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        """關閉 AsyncClient，釋放連線池資源"""
        if self._client:
            await self._client.aclose()
            self._client = None

    # ── 內部工具方法 ───────────────────────────────────────────────────

    def _ensure_client(self) -> httpx.AsyncClient:
        """確認 AsyncClient 已初始化（必須在 async with 區塊內使用）"""
        if self._client is None:
            raise RuntimeError(
                "OneCampusClient 必須透過 'async with' 使用，"
                "以確保 HTTP 連線池正確初始化與釋放。"
            )
        return self._client

    def _require_login(self) -> None:
        """確認已完成登入，否則拋出 NotLoggedInError"""
        if not self._access_token and not self._session_cookie:
            raise NotLoggedInError(
                "尚未登入，請先執行 await client.login(username, password)"
            )

    def _auth_headers(self) -> dict[str, str]:
        """
        組合已登入後所需的驗證標頭。

        1campus 常見兩種驗證方式：
          A. JWT Bearer Token → Authorization: Bearer <token>
          B. Session Cookie   → Cookie: <session_key>=<value>

        若兩者皆有，優先使用 Token（通常安全性較高）。
        """
        if self._access_token:
            return {"Authorization": f"Bearer {self._access_token}"}
        if self._session_cookie:
            return {"Cookie": self._session_cookie}
        return {}

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        timeout: float,
        **kwargs: Any,
    ) -> httpx.Response:
        """
        發出 HTTP 請求，內建三種異常處理：

        ① 401 未授權  → 清除 Token，立即拋出 AuthError（不重試）
        ② 429 頻率限制 → 讀取 Retry-After 標頭，指數退避後重試
        ③ Timeout     → 指數退避後重試，超過 max_retries 則拋出 FetchTimeoutError

        其他非 2xx 狀態碼會由 httpx 的 raise_for_status() 拋出 HTTPStatusError。
        """
        client = self._ensure_client()
        last_exc: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                resp = await client.request(method, url, timeout=timeout, **kwargs)

                # ── ① HTTP 401：Token 無效或已過期 ───────────────────
                if resp.status_code == 401:
                    self._access_token   = None
                    self._session_cookie = None
                    raise AuthError(
                        "驗證失敗：請檢查帳號密碼是否正確，"
                        "或 Token 已過期，請重新登入。"
                    )

                # ── ② HTTP 429：請求頻率超限 ──────────────────────────
                if resp.status_code == 429:
                    if attempt >= self.max_retries:
                        raise RateLimitError(
                            f"請求頻率超限（HTTP 429），"
                            f"已重試 {self.max_retries} 次仍失敗：{url}"
                        )
                    # 優先使用伺服器指定的等待秒數，否則指數退避
                    retry_after = int(resp.headers.get("Retry-After", 2 ** attempt))
                    logger.warning(
                        "[429] 頻率限制，%d 秒後進行第 %d 次重試（%s）",
                        retry_after, attempt + 1, url,
                    )
                    await asyncio.sleep(retry_after)
                    continue

                resp.raise_for_status()
                return resp

            except httpx.TimeoutException as exc:
                # ── ③ 逾時：指數退避後重試 ────────────────────────────
                last_exc = exc
                if attempt >= self.max_retries:
                    raise FetchTimeoutError(
                        f"請求逾時（>{timeout}s），"
                        f"請確認校務系統是否正常運作：{url}"
                    ) from exc
                wait = 2 ** attempt
                logger.warning(
                    "[Timeout] 第 %d 次逾時，%d 秒後重試（%s）",
                    attempt + 1, wait, url,
                )
                await asyncio.sleep(wait)

        raise FetchTimeoutError("請求失敗，已超過最大重試次數") from last_exc


    # ════════════════════════════════════════════════════════════════════
    # 登入流程
    # ════════════════════════════════════════════════════════════════════

    async def login(self, username: str, password: str) -> None:
        """
        非同步登入 1campus，成功後將憑證存入 self._access_token（或 _session_cookie）。

        流程分兩步：
          步驟 ① — DSNS 查詢，取得學校專屬 API Base URL
          步驟 ② — POST 帳號密碼，取得 Access Token / Session Cookie

        ┌──────────────────────────────────────────────────────────────┐
        │  如何用 Chrome DevTools 找到真實 API：                       │
        │                                                              │
        │  1. 開啟學校 1campus 登入頁，按 F12 → Network 分頁          │
        │  2. 勾選 "Preserve log"，輸入帳密後按登入                   │
        │  3. 在 Network 清單中尋找：                                  │
        │     - 狀態碼 200 且回應含 "token" 或 "access_token" 的請求  │
        │     - 或狀態碼 302 且回應含 "Set-Cookie" 標頭的請求         │
        │  4. 右鍵 → "Copy as cURL"，即可取得完整 URL、Headers、Body  │
        └──────────────────────────────────────────────────────────────┘
        """
        self._ensure_client()
        logger.info("[登入] 啟動安全遠端驗證通道... DSNS: %s", self.dsns_domain)

        # ── 步驟 ①：DSNS 路由查詢（取得學校專屬 API 端點）────────────
        #
        # 1campus 使用 DSNS 將學校代碼映射到各校後端伺服器。
        # 若你的學校使用固定網域（如 https://tp.1campus.net），
        # 可跳過此步驟，直接設定：
        #   self._api_base = f"https://{self.dsns_domain}"
        #
        # ★ 真實 URL（從 DevTools 觀察，尋找 dsns 或 lookup 相關請求）：
        #   url = self._DSNS_LOOKUP_URL
        #
        # ★ 真實 Payload（JSON 格式範例）：
        #   json = {"dsns": self.dsns_domain}
        #
        # ★ 真實 Headers（從 DevTools Request Headers 複製）：
        #   headers = {"Content-Type": "application/json"}
        #
        # ★ 真實呼叫範例（替換下方 sleep）：
        #   resp = await self._request_with_retry(
        #       "POST",
        #       self._DSNS_LOOKUP_URL,
        #       json={"dsns": self.dsns_domain},
        #       timeout=self.login_timeout,
        #   )
        #   dsns_data = resp.json()
        #   self._api_base = dsns_data["api_base"]  # 依實際回應欄位調整
        #
        # --- 目前為模擬，替換時刪除以下兩行 ---
        await asyncio.sleep(0.1)  # 模擬 DSNS 查詢網路延遲
        self._api_base = f"https://{self.dsns_domain}/api"  # 模擬 DSNS 回應

        logger.info("[登入] DSNS 路由完成，API Base: %s", self._api_base)

        # ── 步驟 ②：POST 帳號密碼，換取 Access Token ──────────────────
        #
        # ★ 真實 URL（從 DevTools 尋找 login / signin / auth / token 相關 POST）：
        #   login_url = f"{self._api_base}/v1/auth/login"
        #
        # ★ 真實 Payload（JSON 格式，欄位名稱依 DevTools Request Body 調整）：
        #   json = {"username": username, "password": password}
        #   或 Form 格式：data = {"account": username, "passwd": password}
        #
        # ★ 真實 Headers（從 DevTools Request Headers 複製，注意 Referer）：
        #   headers = {
        #       "Content-Type":      "application/json",
        #       "X-Requested-With":  "XMLHttpRequest",
        #       "Referer":           f"https://{self.dsns_domain}/login",
        #       "Origin":            f"https://{self.dsns_domain}",
        #   }
        #
        # ★ 如果 1campus 使用 JWT（Token-based）：
        #   resp = await self._request_with_retry(
        #       "POST",
        #       f"{self._api_base}/v1/auth/login",
        #       json={"username": username, "password": password},
        #       headers={"Referer": f"https://{self.dsns_domain}/login"},
        #       timeout=self.login_timeout,
        #   )
        #   body = resp.json()
        #   self._access_token = body["access_token"]  # 欄位名稱依實際回應調整
        #
        # ★ 如果 1campus 使用 Session Cookie：
        #   resp = await self._request_with_retry(
        #       "POST",
        #       f"{self._api_base}/v1/auth/login",
        #       data={"account": username, "passwd": password},
        #       timeout=self.login_timeout,
        #   )
        #   # HTTPX 的 follow_redirects=True 會自動跟隨 302，Cookie 已被 client 儲存
        #   # 若需手動保存 Cookie 字串：
        #   self._session_cookie = resp.headers.get("Set-Cookie", "")
        #
        # --- 目前為模擬，替換時刪除以下程式碼區塊 ---
        if not username or not password:
            raise AuthError(
                "驗證失敗：請檢查帳號密碼是否正確，或遠端連線是否中斷。"
            )
        await asyncio.sleep(0.1)  # 模擬登入 POST 網路延遲
        # 模擬伺服器核發的 JWT Token
        self._access_token = f"mock_jwt.{username}.abc123xyz"
        # --- 模擬結束 ---

        logger.info("[登入] 驗證成功，Token 已快取至 Session。")

    # ════════════════════════════════════════════════════════════════════
    # 各資料端點（私有方法，由 fetch_all_data 並發呼叫）
    # ════════════════════════════════════════════════════════════════════

    async def _fetch_profile(self) -> dict:
        """
        撈取學生基本資料（姓名、班級、座號、性別、學號）。

        ★ 如何找到真實端點：
          DevTools → Network → 篩選關鍵字 "profile" / "student" / "basic"
          尋找登入後自動發出的 GET 請求

        ★ 真實呼叫範例（替換下方 sleep + return）：
            resp = await self._request_with_retry(
                "GET",
                f"{self._api_base}/v1/student/profile",
                headers=self._auth_headers(),
                timeout=self.fetch_timeout,
            )
            return resp.json()

        ★ 常見回應欄位（依實際 1campus 回應調整）：
            {
              "name":       "綾小路 清隆",
              "class":      "2D",
              "seat":       7,
              "gender":     "M",
              "student_id": "S11200007"
            }
        """
        await asyncio.sleep(0.1)  # ← 模擬網路延遲，真實版請刪除此行
        return {
            "name":       "綾小路 清隆",
            "romaji":     "AYANOKOJI KIYOTAKA",
            "class":      "D",
            "year":       2,
            "seat":       7,
            "gender":     "M",
            "student_id": "S11200007",
        }

    async def _fetch_grades(self) -> dict:
        """
        撈取各科成績（學期成績、特別考試分數）。

        ★ 如何找到真實端點：
          DevTools → Network → 篩選關鍵字 "grade" / "score" / "exam"

        ★ 真實呼叫範例（替換下方 sleep + return）：
            resp = await self._request_with_retry(
                "GET",
                f"{self._api_base}/v1/student/grades",
                params={"year": "112", "term": "1"},  # 學年度與學期，依實際調整
                headers=self._auth_headers(),
                timeout=self.fetch_timeout,
            )
            return resp.json()

        ★ 常見回應結構：
            {
              "average":      61.8,
              "stem_average": 56.5,
              "subjects": [
                {"subject": "數學",  "first_term": 78, "second_term": null, "special": 100},
                {"subject": "英文",  "first_term": 48, "second_term": 55,   "special": null}
              ]
            }
        """
        await asyncio.sleep(0.1)  # ← 模擬網路延遲
        return {
            "average":      61.8,
            "stem_average": 56.5,
            "subjects": [
                {"subject": "數學",     "first_term": 78, "second_term": None, "special": 100},
                {"subject": "現代國文", "first_term": 52, "second_term": 50,   "special": None},
                {"subject": "英文",     "first_term": 48, "second_term": 55,   "special": None},
                {"subject": "物理",     "first_term": 62, "second_term": None, "special": None},
            ],
        }

    async def _fetch_conduct(self) -> dict:
        """
        撈取獎懲紀錄與出缺席紀錄。

        ★ 如何找到真實端點：
          DevTools → Network → 篩選關鍵字 "conduct" / "attend" / "reward" / "punish"

        ★ 注意：某些版本的 1campus 將獎懲與出缺席拆成兩支 API，
          若如此，請在 fetch_all_data() 中分別建立兩個 Task，
          並在此合併結果後回傳。

        ★ 真實呼叫範例（替換下方 sleep + return）：
            resp = await self._request_with_retry(
                "GET",
                f"{self._api_base}/v1/student/conduct",
                headers=self._auth_headers(),
                timeout=self.fetch_timeout,
            )
            return resp.json()

        ★ 常見回應結構：
            {
              "absent_days":  2,
              "late_count":   1,
              "rewards":      [{"type": "嘉獎", "count": 3, "reason": "服務學習優良"}],
              "punishments":  []
            }
        """
        await asyncio.sleep(0.1)  # ← 模擬網路延遲
        return {
            "absent_days": 2,
            "late_count":  1,
            "rewards": [
                {"type": "嘉獎", "count": 3, "reason": "服務學習表現優良"},
            ],
            "punishments": [],
        }

    async def _fetch_positions(self) -> dict:
        """
        撈取幹部資訊（班級幹部、社團幹部、學生會職務）。

        ★ 如何找到真實端點：
          DevTools → Network → 篩選關鍵字 "position" / "cadre" / "club" / "council"

        ★ 真實呼叫範例（替換下方 sleep + return）：
            resp = await self._request_with_retry(
                "GET",
                f"{self._api_base}/v1/student/positions",
                headers=self._auth_headers(),
                timeout=self.fetch_timeout,
            )
            return resp.json()

        ★ 常見回應結構：
            {
              "class_positions":  ["班長"],
              "club_positions":   [{"club": "科學研究社", "title": "社長"}],
              "student_council":  null
            }
        """
        await asyncio.sleep(0.1)  # ← 模擬網路延遲
        return {
            "class_positions": [],
            "club_positions":  [],
            "student_council": None,
        }

    # ════════════════════════════════════════════════════════════════════
    # 並發撈取主方法
    # ════════════════════════════════════════════════════════════════════

    async def fetch_all_data(self) -> StudentAllData:
        """
        並發撈取學生所有資料（基本資料、各科成績、獎懲出缺席、幹部資訊）。

        ┌──────────────────────────────────────────────────────────────┐
        │  速度原理（為什麼比 Puppeteer / 循序請求快？）               │
        │                                                              │
        │  循序請求：                                                  │
        │    [基本資料] → [成績] → [獎懲] → [幹部]                    │
        │    耗時 ≈ 4 × 平均單次延遲（串行等待）                      │
        │                                                              │
        │  asyncio.gather 並發：                                       │
        │    [基本資料]                                                │
        │    [成績    ] ← 四個請求同時出發                            │
        │    [獎懲    ]                                                │
        │    [幹部    ]                                                │
        │    耗時 ≈ 最慢那一個請求的延遲（並行等待）                  │
        │    → 速度理論提升約 4 倍                                     │
        └──────────────────────────────────────────────────────────────┘

        拋出：
            NotLoggedInError  — 尚未登入
            AuthError         — 任一 Task 收到 HTTP 401
            FetchTimeoutError — 任一 Task 請求逾時超過重試上限
            RateLimitError    — 任一 Task 超過頻率限制重試上限
        """
        self._require_login()
        logger.info("[撈取] 啟動並發撈取，共 4 個端點...")

        # asyncio.gather 同時觸發所有請求
        # return_exceptions=False（預設）：任一 Task 拋出例外，立即向上傳播
        # 若希望部分失敗不影響其他端點，改為 return_exceptions=True，
        # 並在下方以 isinstance(result, Exception) 逐一判斷
        profile, grades, conduct, positions = await asyncio.gather(
            self._fetch_profile(),
            self._fetch_grades(),
            self._fetch_conduct(),
            self._fetch_positions(),
        )

        logger.info("[撈取] 四端點全部完成，資料已整合。")

        return StudentAllData(
            profile=profile,
            grades=grades,
            conduct=conduct,
            positions=positions,
        )

    # ════════════════════════════════════════════════════════════════════
    # 便利方法
    # ════════════════════════════════════════════════════════════════════

    async def login_and_fetch(self, username: str, password: str) -> StudentAllData:
        """
        登入後立即並發撈取所有資料，適合一次性使用情境。

        使用範例：
            async with OneCampusClient("tp001.1campus.net") as client:
                data = await client.login_and_fetch("帳號", "密碼")
                print(data.profile["name"])
        """
        await self.login(username, password)
        return await self.fetch_all_data()

    def __repr__(self) -> str:
        status = "已登入" if (self._access_token or self._session_cookie) else "未登入"
        return f"OneCampusClient(dsns={self.dsns_domain!r}, status={status})"


# ═══════════════════════════════════════════════════════════════════════
# 臺中市校務系統（school.tc.edu.tw）API 存取
# 認證方式：cc_session cookie（使用者在 1campus webview 建立 session 後取得）
# ═══════════════════════════════════════════════════════════════════════

_SCHOOL_BASE = "https://school.tc.edu.tw/csp/webview"


async def fetch_school_grades(cc_session: str, year: int = 114, semester: int = 2) -> list[dict]:
    """取得學生階段成績，回傳各科目資料串列（含 科目平均、時數）"""
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, verify=False) as c:
        r = await c.get(
            f"{_SCHOOL_BASE}/student/score/get-section-score",
            params={"year": year, "semester": semester, "test_sort": 1},
            cookies={"cc_session": cc_session},
            headers={"Referer": f"{_SCHOOL_BASE}/"},
        )
        r.raise_for_status()
        body = r.json()
        data = body.get("data") or {}
        return data.get("階段成績", [])


async def fetch_school_conduct(cc_session: str, year: int = 114, semester: int = 2) -> dict:
    """取得學生獎懲統計（嘉獎、小功、大功、警告、小過、大過 次數）"""
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, verify=False) as c:
        r = await c.get(
            f"{_SCHOOL_BASE}/student/student-reward/count",
            params={"year": year, "semester": semester},
            cookies={"cc_session": cc_session},
            headers={"Referer": f"{_SCHOOL_BASE}/"},
        )
        r.raise_for_status()
        data = r.json().get("data") or []
        return data[0] if data else {}


def calc_academic(subjects: list[dict]) -> float:
    """學業分數：以時數加權平均各科 科目平均，回傳 0–100"""
    total_hours = sum(s.get("時數", 1) for s in subjects)
    if not total_hours:
        return 0.0
    weighted = sum(s.get("科目平均", 0) * s.get("時數", 1) for s in subjects)
    return round(weighted / total_hours, 1)


def calc_conduct(counts: dict) -> float:
    """行動力分數：基準 70 加減獎懲積分，夾限 0–100。
    嘉獎 +1、小功 +3、大功 +9、警告 -1、小過 -3、大過 -9"""
    score = 70.0 + (
        counts.get("嘉獎", 0) * 1 +
        counts.get("小功", 0) * 3 +
        counts.get("大功", 0) * 9 -
        counts.get("警告", 0) * 1 -
        counts.get("小過", 0) * 3 -
        counts.get("大過", 0) * 9
    )
    return round(min(100.0, max(0.0, score)), 1)


# ═══════════════════════════════════════════════════════════════════════
# 快速測試入口（python onecampus_client.py）
# ═══════════════════════════════════════════════════════════════════════

async def _demo() -> None:
    """示範完整登入 + 並發撈取流程（使用模擬資料）"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    print("\n─── OneCampusClient 模擬測試 ───\n")

    async with OneCampusClient(
        dsns_domain="tp001.1campus.net",
        fetch_timeout=20.0,
        max_retries=3,
    ) as client:

        # ① 登入
        await client.login(username="testuser", password="testpass")
        print(f"客戶端狀態：{client!r}\n")

        # ② 並發撈取
        import time
        t0 = time.perf_counter()
        data = await client.fetch_all_data()
        elapsed = time.perf_counter() - t0

        # ③ 輸出結果
        print(f"撈取完成（耗時 {elapsed:.3f}s）\n")
        print("【基本資料】")
        for k, v in data.profile.items():
            print(f"  {k}: {v}")
        print(f"\n【成績摘要】平均 {data.grades['average']}，理科平均 {data.grades['stem_average']}")
        print(f"\n【出缺席】缺席 {data.conduct['absent_days']} 天，遲到 {data.conduct['late_count']} 次")
        print(f"\n【幹部資訊】班級：{data.positions['class_positions'] or '無'}")

    # ④ 驗證異常處理
    print("\n─── 異常處理驗證 ───\n")
    async with OneCampusClient("tp001.1campus.net") as client:
        try:
            await client.fetch_all_data()  # 未登入直接撈取
        except NotLoggedInError as e:
            print(f"[NotLoggedInError] OK  {e}")

        try:
            await client.login(username="", password="")  # 空帳密
        except AuthError as e:
            print(f"[AuthError]        OK  {e}")


if __name__ == "__main__":
    asyncio.run(_demo())
