"""키움증권(Kiwoom) REST API 클라이언트. kis_price_client.py의 KIS 버전과 같은 역할이지만,
헤더 이름·인증 방식·엔드포인트 구조가 KIS와 달라서 별도 모듈로 새로 만든다.

키움 vs KIS 주요 차이점(2026-07-24 공식 API 스펙 JSON 기준으로 확인):
- TR 식별 헤더가 tr_id가 아니라 api-id.
- 모든 API가 GET이 아니라 POST + JSON body (조회성 API도 마찬가지).
- 토큰 발급 필드명이 appkey/secretkey (KIS는 appkey/appsecret), 응답 필드도 access_token이
  아니라 그냥 token.
- 국내(dostk)와 해외(us)가 완전히 다른 URL 프리픽스(/api/dostk/*, /api/us/*)와 파라미터
  이름 체계(dmst_stex_tp vs stex_tp, KRX/NXT/SOR vs NA/ND/NY)를 씀.
- 키 발급 자체가 KIS와 다르게 실전/모의(국내)/모의(해외) 3세트로 완전히 분리되어 있음
  (계좌번호도 다 다름) — 그래서 자격증명 선택 로직이 KIS보다 한 단계 더 필요하다.

인증 환경변수 (2026-07-24 발급, Windows 사용자 환경변수로 저장됨):
- KIWOOM_APP_KEY / KIWOOM_APP_SECRET             — 실전투자 (계좌 6107-9193)
- KIWOOM_PAPER_APP_KEY / KIWOOM_PAPER_APP_SECRET — 모의투자(국내, 계좌 81312864)
- KIWOOM_PAPER_OVERSEAS_APP_KEY / KIWOOM_PAPER_OVERSEAS_APP_SECRET — 해외모의투자(계좌 61110872)
"""

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

REAL_BASE_URL = "https://api.kiwoom.com"
DEMO_BASE_URL = "https://mockapi.kiwoom.com"


@dataclass(frozen=True)
class Credential:
    """계정별로 등록한 앱키/시크릿을 쓸 때(2026-08-12, 로그인+계좌관리 기능 추가) 넘기는
    오버라이드. 안 넘기면(cred=None) 기존처럼 mode(demo/real)로 env var를 찾는다 — 그래서
    이 프로젝트의 다른 스크립트들은 아무것도 안 바꿔도 그대로 동작한다.
    cache_key는 계좌마다 달라야 토큰 캐시가 안 섞인다(예: f"account:{account_id}")."""

    app_key: str
    app_secret: str
    cache_key: str

MIN_CALL_INTERVAL_SECONDS = 1.0  # 실측(2026-07-24): 0.3초 간격으로 5~6번째 호출에서 곧장 429 발생 — KIS 수준으로 늘림

TOKEN_CACHE_DIR = Path(__file__).resolve().parent
_last_call_at = 0.0
_token_cache: dict = {}  # cache_key -> {"token":..., "expires_dt":...}


def _throttle() -> None:
    global _last_call_at
    elapsed = time.monotonic() - _last_call_at
    if elapsed < MIN_CALL_INTERVAL_SECONDS:
        time.sleep(MIN_CALL_INTERVAL_SECONDS - elapsed)
    _last_call_at = time.monotonic()


def _credentials(mode: str, market: str, cred: Credential | None = None) -> tuple[str, str]:
    """mode: "real" | "demo", market: "domestic" | "overseas".

    실전은 국내/해외 구분 없이 키 하나(KIWOOM_APP_KEY)를 쓰고, 모의투자는 국내용과
    해외용 키가 서로 다르다(키움이 실제로 그렇게 발급함 — KIS는 모의 키 하나로 둘 다 커버했었음).
    """
    if cred is not None:
        return cred.app_key, cred.app_secret
    if mode == "real":
        key, secret = os.environ.get("KIWOOM_APP_KEY"), os.environ.get("KIWOOM_APP_SECRET")
    elif mode == "demo" and market == "domestic":
        key, secret = os.environ.get("KIWOOM_PAPER_APP_KEY"), os.environ.get("KIWOOM_PAPER_APP_SECRET")
    elif mode == "demo" and market == "overseas":
        key, secret = os.environ.get("KIWOOM_PAPER_OVERSEAS_APP_KEY"), os.environ.get("KIWOOM_PAPER_OVERSEAS_APP_SECRET")
    else:
        raise ValueError(f"알 수 없는 mode/market 조합: {mode}/{market}")
    if not key or not secret:
        raise RuntimeError(f"Kiwoom 자격증명 환경변수가 비어있음 (mode={mode}, market={market})")
    return key, secret


def _base_url(mode: str) -> str:
    return REAL_BASE_URL if mode == "real" else DEMO_BASE_URL


def _cache_key(mode: str, market: str, cred: Credential | None) -> str:
    return cred.cache_key if cred is not None else f"{mode}:{market}"


def _token_cache_path(mode: str, market: str, cred: Credential | None = None) -> Path:
    safe_key = _cache_key(mode, market, cred).replace(":", "_")
    return TOKEN_CACHE_DIR / f".kiwoom_token_cache_{safe_key}.json"


def _is_still_valid(cached: dict) -> bool:
    # expires_dt 포맷: YYYYMMDDHHMMSS (KIS의 access_token_token_expired와 같은 자릿수 관례를 따름 — 실제
    # 응답 받아보고 포맷이 다르면 여기만 고치면 됨)
    try:
        expires_at = datetime.strptime(cached["expires_dt"], "%Y%m%d%H%M%S")
    except (KeyError, ValueError):
        return False
    return expires_at - datetime.now() > timedelta(minutes=5)


def _invalidate_token(mode: str, market: str, cred: Credential | None = None) -> None:
    """캐시된 토큰이 만료시각(expires_dt)과 무관하게 서버에서 거부되는 경우가 실측으로
    확인됨(2026-07-25/26, "인증에 실패했습니다[8005:Token이 유효하지 않습니다]") — 원인은
    아직 불명확하지만, 이 에러를 만나면 캐시를 지우고 새 토큰을 발급받도록 한다."""
    cache_key = _cache_key(mode, market, cred)
    _token_cache.pop(cache_key, None)
    _token_cache_path(mode, market, cred).unlink(missing_ok=True)


def _issue_token(mode: str, market: str, cred: Credential | None = None) -> str:
    cache_key = _cache_key(mode, market, cred)
    if cache_key in _token_cache:
        return _token_cache[cache_key]["token"]

    cache_path = _token_cache_path(mode, market, cred)
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if _is_still_valid(cached):
            _token_cache[cache_key] = cached
            return cached["token"]
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass

    app_key, app_secret = _credentials(mode, market, cred)
    _throttle()
    response = requests.post(
        f"{_base_url(mode)}/oauth2/token",
        headers={"content-type": "application/json;charset=UTF-8"},
        data=json.dumps({
            "grant_type": "client_credentials",
            "appkey": app_key,
            "secretkey": app_secret,
        }),
        timeout=20,
    )
    response.raise_for_status()
    body = response.json()
    if "token" not in body:
        raise RuntimeError(f"Kiwoom 토큰 발급 실패 (mode={mode}, market={market}): {body}")

    cache_path.write_text(json.dumps(body), encoding="utf-8")
    _token_cache[cache_key] = body
    return body["token"]


def _headers(mode: str, market: str, api_id: str, cred: Credential | None = None) -> dict:
    return {
        "content-type": "application/json;charset=UTF-8",
        "authorization": f"Bearer {_issue_token(mode, market, cred)}",
        "api-id": api_id,
        "cont-yn": "N",
        "next-key": "",
    }


def _is_invalid_token_error(data: dict) -> bool:
    msg = str(data.get("return_msg", ""))
    return "Token" in msg and ("유효하지" in msg or "인증에 실패" in msg)


def _post(
    mode: str, market: str, api_id: str, path: str, body: dict,
    retries: int = 2, cred: Credential | None = None,
) -> dict:
    """모든 키움 API가 조회든 주문이든 POST + JSON body라는 게 KIS와의 가장 큰 차이.

    429(rate limit)는 실측으로 초당 1회 스로틀로도 연속 호출 시 발생함 — KIS처럼 짧게
    재시도한다(kis_price_client._get_with_retry와 동일한 완화책).

    토큰이 만료시각과 무관하게 서버에서 거부되는 경우가 있어서(2026-07-25/26 실측,
    "Token이 유효하지 않습니다"), 이 에러를 받으면 캐시를 지우고 새 토큰으로 한 번 더
    시도한다 — 안 그러면 GitHub Actions 자동화가 이 에러를 만난 이후로 계속 조용히
    실패만 반복하게 된다(캐시가 다음 실행에도 그대로 남아있어서)."""
    for token_attempt in range(2):
        response = None
        for attempt in range(retries + 1):
            _throttle()
            response = requests.post(
                f"{_base_url(mode)}{path}",
                headers=_headers(mode, market, api_id, cred),
                data=json.dumps(body),
                timeout=20,
            )
            if response.status_code != 429:
                break
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
        response.raise_for_status()
        data = response.json()
        if data.get("return_code") in (0, "0", None):
            return data
        if token_attempt == 0 and _is_invalid_token_error(data):
            _invalidate_token(mode, market, cred)
            continue
        raise RuntimeError(f"Kiwoom API 실패 {api_id} ({mode}/{market}): {data}")


def _post_readonly(
    mode: str, market: str, api_id: str, path: str, body: dict,
    retries: int = 2, cred: Credential | None = None,
) -> dict:
    """조회(읽기전용) API 전용 래퍼 — read timeout이 났을 때만 짧게 재시도한다
    (2026-08-15 실측: KIS 쪽에서 장마감 무렵 read timeout으로 잔고 스냅샷 스크립트가
    통째로 죽은 사례가 있어, 같은 위험이 있는 키움 조회 API에도 적용).

    주문 생성/정정/취소(place_domestic_order 등)에는 절대 쓰지 않는다 — 타임아웃은
    "요청이 서버에 실제로 도달해 처리됐는지 알 수 없는" 상태라, 거기서 재시도하면 같은
    주문이 중복 접수될 위험이 있다. 그래서 이 함수는 아래 조회용 함수들에서만 부른다."""
    last_error = None
    for attempt in range(retries + 1):
        try:
            return _post(mode, market, api_id, path, body, cred=cred)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    raise last_error


def abs_price(signed_value) -> float:
    """cur_prc/open_pric/high_pric 등 시세 필드는 "부호가 포함된 숫자"로 내려온다 —
    부호는 전일대비 등락 방향(양수=상승, 음수=하락)이지 가격이 실제로 음수라는 뜻이 아니다.
    실제 가격이 필요하면 이 함수로 부호를 떼고 절대값만 취할 것(실측 확인: 2026-07-24,
    472150이 -20815, TQQQ가 -65.7400으로 내려옴 — 둘 다 그날 하락 중이었을 뿐 가격은 양수)."""
    return abs(float(signed_value))


# ── 국내주식 ────────────────────────────────────────────────────────────────

def get_domestic_quote(stk_cd: str, mode: str = "demo", cred: Credential | None = None) -> dict:
    """주식기본정보요청(ka10001) — 현재가 등 기본 정보. cur_prc가 현재가(원) — 부호 포함이니
    실제 가격이 필요하면 abs_price(quote["cur_prc"])로 쓸 것."""
    return _post_readonly(mode, "domestic", "ka10001", "/api/dostk/stkinfo", {"stk_cd": stk_cd}, cred=cred)


def get_domestic_orderbook(stk_cd: str, mode: str = "demo", cred: Credential | None = None) -> dict:
    """주식호가요청(ka10004) — 10단 호가."""
    return _post_readonly(mode, "domestic", "ka10004", "/api/dostk/mrkcond", {"stk_cd": stk_cd}, cred=cred)


def place_domestic_order(
    stk_cd: str, side: str, qty: int, price: int | None = None,
    trde_tp: str = "0", dmst_stex_tp: str = "KRX", mode: str = "demo",
    cred: Credential | None = None,
) -> dict:
    """side: "buy" | "sell". trde_tp 기본값 "0"=보통(지정가). price=None이면 시장가로 간주하고
    ord_uv를 빈 문자열로 보낸다(trde_tp도 "3"으로 바꿔줘야 함 — 호출부에서 지정)."""
    api_id = "kt10000" if side == "buy" else "kt10001"
    body = {
        "dmst_stex_tp": dmst_stex_tp,
        "stk_cd": stk_cd,
        "ord_qty": str(qty),
        "ord_uv": str(price) if price is not None else "",
        "trde_tp": trde_tp,
        "cond_uv": "",
    }
    return _post(mode, "domestic", api_id, "/api/dostk/ordr", body, cred=cred)


def amend_domestic_order(
    orig_ord_no: str, stk_cd: str, qty: int, price: int,
    dmst_stex_tp: str = "KRX", mode: str = "demo", cred: Credential | None = None,
) -> dict:
    """정정주문(kt10002). qty=0이면 잔량 전부 정정. 정정 시 새 주문번호가 반환됨(KIS와 동일한 특성)."""
    body = {
        "dmst_stex_tp": dmst_stex_tp,
        "orig_ord_no": orig_ord_no,
        "stk_cd": stk_cd,
        "mdfy_qty": str(qty),
        "mdfy_uv": str(price),
        "mdfy_cond_uv": "",
    }
    return _post(mode, "domestic", "kt10002", "/api/dostk/ordr", body, cred=cred)


def cancel_domestic_order(
    orig_ord_no: str, stk_cd: str, qty: int = 0,
    dmst_stex_tp: str = "KRX", mode: str = "demo", cred: Credential | None = None,
) -> dict:
    """취소주문(kt10003). qty=0이면 잔량 전부 취소."""
    body = {
        "dmst_stex_tp": dmst_stex_tp,
        "orig_ord_no": orig_ord_no,
        "stk_cd": stk_cd,
        "cncl_qty": str(qty),
    }
    return _post(mode, "domestic", "kt10003", "/api/dostk/ordr", body, cred=cred)


def get_domestic_unfilled_orders(stk_cd: str = "", mode: str = "demo", cred: Credential | None = None) -> dict:
    """미체결요청(ka10075)."""
    body = {
        "all_stk_tp": "1" if stk_cd else "0",
        "trde_tp": "0",
        "stk_cd": stk_cd,
        "stex_tp": "0",
    }
    return _post_readonly(mode, "domestic", "ka10075", "/api/dostk/acnt", body, cred=cred)


def get_domestic_filled_orders(
    stk_cd: str = "", ord_no: str = "", mode: str = "demo", cred: Credential | None = None,
) -> dict:
    """체결요청(ka10076)."""
    body = {
        "stk_cd": stk_cd,
        "qry_tp": "1" if stk_cd else "0",
        "sell_tp": "0",
        "ord_no": ord_no,
        "stex_tp": "0",
    }
    return _post_readonly(mode, "domestic", "ka10076", "/api/dostk/acnt", body, cred=cred)


def get_domestic_cash_balance(mode: str = "demo", cred: Credential | None = None) -> dict:
    """예수금상세현황요청(kt00001)."""
    return _post_readonly(mode, "domestic", "kt00001", "/api/dostk/acnt", {"qry_tp": "2"}, cred=cred)


def get_domestic_holdings(mode: str = "demo", cred: Credential | None = None) -> dict:
    """계좌평가잔고내역요청(kt00018)."""
    return _post_readonly(
        mode, "domestic", "kt00018", "/api/dostk/acnt",
        {"qry_tp": "1", "dmst_stex_tp": "KRX"}, cred=cred,
    )


# ── 해외주식 ────────────────────────────────────────────────────────────────

EXCHANGE_BY_TICKER = {
    "TQQQ": "ND",
    "QQQ": "ND",
    "SPY": "NY",
    "VIXY": "NA",
    "IEF": "ND",
    "HYG": "NY",  # 실측(2026-07-24): NA(AMEX)로는 "종목 정보가 없습니다" 실패, NY(NYSE Arca 취급)라야 됨
    "LQD": "NY",  # 위와 동일한 이유
    "GPIQ": "ND",  # 실계좌 실보유 종목(2026-07-25 확인)
    "IREN": "ND",  # 실계좌 실보유 종목(2026-07-25 확인)
    "SCHD": "NY",  # 실계좌 실보유 종목(2026-07-29 확인) — 슈왑 미국배당주 ETF
    "SCHG": "NY",  # 실계좌 실보유 종목(2026-07-29 확인) — 슈왑 대형성장주 ETF
    "QLD": "NY",  # 실계좌 실보유 종목(2026-07-29 확인) — ProShares Ultra QQQ(2배), TQQQ와 같은 계열이지만 ND 아님, 실측 필요했음
    "TSLA": "ND",  # 실계좌 실보유 종목(2026-07-29 확인)
    "AAPL": "ND",  # 실계좌 실보유 종목(2026-08-10 확인, 0.01주 소수점 잔량이라 일부 HTS 화면엔 안 보임)

    # 2026-07-29: 사용자가 공유한 "미국종목 티커 모음"(시가총액 상위 100개 ETF) 시트 기준으로
    # 조회·주문 가능하게 일괄 추가. 전부 get_overseas_quote로 ND/NY/NA 세 거래소를 순서대로
    # 찔러서 실측 확인한 결과만 넣었다(추측 없음) — SPLG는 세 거래소 전부 "종목 정보가
    # 없습니다[1903]"로 일관되게 실패해서 Kiwoom이 아예 취급하지 않는 것으로 보고 제외함.
    # 주의: 이 목록은 조회·주문용일 뿐, 잔고 새로고침 대상은 OVERSEAS_HOLDINGS_TICKERS(위쪽,
    # order_console.py)에 따로 있는 실제 보유 종목 목록이다 — 여기에 아무리 추가해도 잔고
    # 화면에는 안 나타난다(그래야 잔고 새로고침이 100개 종목을 순서대로 조회하느라 느려지지
    # 않는다).
    "VOO": "NY", "IVV": "NY", "VTI": "NY", "VUG": "NY", "VEA": "NY", "VTV": "NY",
    "IEFA": "NA", "BND": "ND", "AGG": "NY", "IWF": "NY", "IJH": "NY", "IJR": "NY",
    "VIG": "NY", "VGT": "NY", "VWO": "NY", "IEMG": "NY", "VXUS": "ND", "GLD": "NY",
    "VO": "NY", "RSP": "NY", "XLK": "NY", "IWM": "NY", "ITOT": "NY", "VB": "NY",
    "IWD": "NY", "BNDX": "ND", "VYM": "NY", "IVW": "NY", "IBIT": "ND", "EFA": "NY",
    "SCHX": "NY", "QUAL": "NA", "XLF": "NY", "TLT": "ND", "VCIT": "ND", "VT": "NY",
    "QQQM": "ND", "VV": "NY", "SCHF": "NY", "IWB": "NY", "MUB": "NY", "IWR": "NY",
    "DIA": "NY", "VEU": "NY", "IXUS": "ND", "XLV": "NY", "JEPI": "NY", "BIL": "NY",
    "MBB": "ND", "VTEB": "NY", "SPYG": "NY", "IVE": "NY", "VNQ": "NY", "IAU": "NY",
    "VCSH": "ND", "XLE": "NY", "SCHB": "NY", "DFAC": "NY", "BSV": "NY", "VBR": "NY",
    "IUSB": "ND", "SGOV": "NY", "DGRO": "NY", "VGIT": "ND", "GOVT": "NA", "JPST": "NY",
    "VONG": "ND", "MGK": "NY", "COWZ": "NA", "SPYV": "NY", "SMH": "ND", "MDY": "NY",
    "XLY": "NY", "SHY": "ND", "USMV": "NA", "VXF": "NY", "SPDW": "NY", "FBTC": "NA",
    "JEPQ": "ND", "XLI": "NY", "IUSG": "ND", "GBTC": "NY", "BIV": "NY", "XLC": "NY",
    "IGSB": "ND", "VGSH": "ND", "IYW": "NY", "VBK": "NY", "DVY": "ND", "SDY": "NY",
    "IUSV": "ND", "EFV": "NA",
    # 제외: SPLG (Kiwoom에서 3개 거래소 전부 "종목 정보가 없습니다[1903]" — 실측 실패)
}


def get_overseas_quote(
    stk_cd: str, exchange: str | None = None, mode: str = "demo", cred: Credential | None = None,
) -> dict:
    """미국주식 현재가 종목정보(usa20100). cur_prc가 현재가(USD, 부호 포함 — abs_price() 참고).
    exchange 생략 시 티커로 추정."""
    stex_tp = exchange or EXCHANGE_BY_TICKER[stk_cd]
    return _post_readonly(mode, "overseas", "usa20100", "/api/us/mrkcond", {"stex_tp": stex_tp, "stk_cd": stk_cd}, cred=cred)


def get_overseas_orderbook(
    stk_cd: str, exchange: str | None = None, mode: str = "demo", cred: Credential | None = None,
) -> dict:
    """미국주식 현재가 10호가(usa20101)."""
    stex_tp = exchange or EXCHANGE_BY_TICKER[stk_cd]
    return _post_readonly(mode, "overseas", "usa20101", "/api/us/mrkcond", {"stex_tp": stex_tp, "stk_cd": stk_cd}, cred=cred)


def place_overseas_order(
    stk_cd: str, side: str, qty: int, price: float | None = None,
    exchange: str | None = None, trde_tp: str = "00", mode: str = "demo",
    cred: Credential | None = None,
) -> dict:
    """side: "buy" | "sell". trde_tp 기본값 "00"=지정가. price=None+trde_tp="03"이면 시장가."""
    api_id = "ust20000" if side == "buy" else "ust20001"
    stex_tp = exchange or EXCHANGE_BY_TICKER[stk_cd]
    body = {
        "stex_tp": stex_tp,
        "stk_cd": stk_cd,
        "ord_qty": str(qty),
        "ord_uv": str(price) if price is not None else "",
        "trde_tp": trde_tp,
    }
    if side == "sell":
        body["stop_pric"] = ""
    return _post(mode, "overseas", api_id, "/api/us/ordr", body, cred=cred)


def amend_overseas_order(
    orig_ord_no: str, stk_cd: str, price: float,
    exchange: str | None = None, mode: str = "demo", cred: Credential | None = None,
) -> dict:
    """정정주문(ust20002). 국내와 달리 수량 정정 필드가 없음 — 가격만 정정 가능."""
    stex_tp = exchange or EXCHANGE_BY_TICKER[stk_cd]
    body = {
        "orig_ord_no": orig_ord_no,
        "stex_tp": stex_tp,
        "stk_cd": stk_cd,
        "mdfy_uv": str(price),
        "stop_pric": "",
    }
    return _post(mode, "overseas", "ust20002", "/api/us/ordr", body, cred=cred)


def cancel_overseas_order(
    orig_ord_no: str, stk_cd: str, exchange: str | None = None, mode: str = "demo",
    cred: Credential | None = None,
) -> dict:
    """취소주문(ust20003)."""
    stex_tp = exchange or EXCHANGE_BY_TICKER[stk_cd]
    body = {"orig_ord_no": orig_ord_no, "stex_tp": stex_tp, "stk_cd": stk_cd}
    return _post(mode, "overseas", "ust20003", "/api/us/ordr", body, cred=cred)


def get_overseas_unfilled_orders(
    stk_cd: str = "", stex_tp: str = "ND", mode: str = "demo", cred: Credential | None = None,
) -> dict:
    """미국주식 원장 미체결(ust21050). stex_tp는 get_overseas_balance와 같은 이유로 필수
    (빈 값이면 "거래소 구분값이 없습니다" 에러) — 2026-09-19 실측으로 확인."""
    body = {"ord_dt": "", "slby_tp": "0", "stex_tp": stex_tp, "stk_cd": stk_cd}
    return _post_readonly(mode, "overseas", "ust21050", "/api/us/acnt", body, cred=cred)


def get_overseas_balance(
    stk_cd: str = "", stex_tp: str = "ND", mode: str = "demo", cred: Credential | None = None,
) -> dict:
    """미국주식 원장잔고확인(ust21070). stex_tp는 실측으로 필수(빈 값이면 "거래소 구분값이
    없습니다" 에러) — 계좌 전체 잔고를 보려면 ND/NY/NA를 각각 호출해서 합쳐야 한다."""
    body = {"stex_tp": stex_tp, "stk_cd": stk_cd}
    return _post_readonly(mode, "overseas", "ust21070", "/api/us/acnt", body, cred=cred)


def get_overseas_cash_balance(mode: str = "demo", cred: Credential | None = None) -> dict:
    """미국주식 예수금 상세(ust21160)."""
    return _post_readonly(mode, "overseas", "ust21160", "/api/us/acnt", {}, cred=cred)


def fetch_live_quote(ticker: str, mode: str = "demo") -> dict:
    """kis_price_client.fetch_live_quote()와 같은 반환 형식({"price":, "as_of":}) —
    live_fg.py/update_live_score.py가 import만 바꿔서 그대로 쓸 수 있게 맞췄다.

    as_of는 실제 체결시각이 아니라 이 함수를 호출한 시점의 wall-clock 시각이다
    (kis_price_client.py와 동일한 한계 — 이전 세션에서 "as_of가 계속 갱신되니 최신"이라고
    잘못 판단했던 실수를 반복하지 말 것). 진짜 체결시각이 필요하면 fetch_live_quote_with_time()을 쓸 것.
    """
    quote = get_overseas_quote(ticker, mode=mode)
    return {"price": abs_price(quote["cur_prc"]), "as_of": pd.Timestamp.now(tz="UTC")}


def fetch_live_quote_with_time(ticker: str, mode: str = "demo") -> dict:
    """usa20101(10호가)의 bid_tm(HH:mm)/dt(YYYYMMDD)을 같이 반환 — 프리마켓 시간대에
    이 시각이 실제로 갱신되는지 보고 "진짜 실시간 데이터인지" 검증하는 용도."""
    quote = get_overseas_orderbook(ticker, mode=mode)
    return {
        "price": abs_price(quote["cur_prc"]),
        "quote_date": quote.get("dt"),
        "quote_time": quote.get("bid_tm"),
        "as_of": pd.Timestamp.now(tz="UTC"),
    }


if __name__ == "__main__":
    print("국내(472150) 현재가:", abs_price(get_domestic_quote("472150")["cur_prc"]))
    print("해외(TQQQ) 현재가:", abs_price(get_overseas_quote("TQQQ")["cur_prc"]))
