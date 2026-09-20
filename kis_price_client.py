"""한국투자증권(KIS) Open API에서 직접 시세를 가져옵니다. Yahoo Finance 대체.

price_fetcher.py와 같은 인터페이스(fetch_live_quote/fetch_price_history)를 제공해서
price_proxy.py / live_fg.py / update_live_score.py가 그대로 쓸 수 있게 합니다.

인증: 환경변수 KIS_PAPER_APP_KEY / KIS_PAPER_APP_SECRET (모의투자 앱키, 조회 전용으로 사용).
토큰은 모듈 로드당 1번만 발급받습니다(1분에 1회 발급 제한 때문에 재사용).
호출 사이에는 초당 호출 제한을 피하기 위해 짧게 대기합니다.

주의: VIX 현물지수는 KIS가 지수 시세로 제공하지 않는다(2026-07-22 확인, inquire_daily_chartprice에
SPX는 정상 응답하지만 VIX는 빈 데이터). 대신 VIX 선물 추종 ETF인 VIXY를 쓴다. VIXY는 콘탱고로
장기적으로 가치가 깎이지만, price_proxy.py의 vix_level 지표는 절대 레벨이 아니라 자기 자신의
252일 rolling z-score라서 방향성(공포 시 급등)만 맞으면 대체 지표로 유효하다.
"""

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

BASE_URL = "https://openapivts.koreainvestment.com:29443"  # 모의투자 서버
MIN_CALL_INTERVAL_SECONDS = 1.0

# 토큰 발급은 1분에 1회 제한이라, 프로세스가 끝나도 재사용할 수 있게 파일에 캐시한다.
TOKEN_CACHE_PATH = Path(__file__).resolve().parent / ".kis_token_cache.json"

# 종목별 KIS 거래소 코드 (2026-07-22 직접 조회로 확인)
EXCHANGE_BY_TICKER = {
    "QQQ": "NAS",
    "VIXY": "AMS",
    "IEF": "NAS",
    "HYG": "AMS",
    "LQD": "AMS",
}

_last_call_at = 0.0
_token_cache: dict = {}


def _throttle() -> None:
    global _last_call_at
    elapsed = time.monotonic() - _last_call_at
    if elapsed < MIN_CALL_INTERVAL_SECONDS:
        time.sleep(MIN_CALL_INTERVAL_SECONDS - elapsed)
    _last_call_at = time.monotonic()


def _get_with_retry(url: str, headers: dict, params: dict, retries: int = 2) -> requests.Response:
    """KIS가 가끔 500(서버 오류)/일시적 rate limit을 던지거나, 아예 응답 없이 read timeout이
    나는 걸 대비해 한두 번 재시도한다(2026-08-15 실측: 장마감 무렵 이 타임아웃 때문에
    잔고 스냅샷 스크립트가 그 자리에서 죽어서 그날 기록이 통째로 안 남았음).

    조회(GET) 전용이다 — 주문 생성/정정/취소 같은 POST에 이 패턴을 그대로 쓰면 안 된다:
    타임아웃은 "요청이 서버에 실제로 도달해 처리됐는지 알 수 없는" 상태라, 거기서
    재시도하면 같은 주문이 중복 접수될 위험이 있다."""
    response = None
    for attempt in range(retries + 1):
        try:
            response = requests.get(url, headers=headers, params=params, timeout=20)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            raise
        if response.status_code < 500 and "초당 거래건수" not in response.text:
            return response
        if attempt < retries:
            time.sleep(2 * (attempt + 1))
    return response


def _app_key() -> str:
    return os.environ["KIS_PAPER_APP_KEY"]


def _app_secret() -> str:
    return os.environ["KIS_PAPER_APP_SECRET"]


def _read_file_cache() -> dict | None:
    try:
        return json.loads(TOKEN_CACHE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _is_still_valid(cached: dict) -> bool:
    expires_at = datetime.strptime(cached["access_token_token_expired"], "%Y-%m-%d %H:%M:%S")
    return expires_at - datetime.now() > timedelta(minutes=5)


def _token() -> str:
    if "access_token" in _token_cache:
        return _token_cache["access_token"]

    cached = _read_file_cache()
    if cached and _is_still_valid(cached):
        _token_cache["access_token"] = cached["access_token"]
        return _token_cache["access_token"]

    response = requests.post(
        f"{BASE_URL}/oauth2/tokenP",
        json={
            "grant_type": "client_credentials",
            "appkey": _app_key(),
            "appsecret": _app_secret(),
        },
        timeout=20,
    )
    response.raise_for_status()
    fresh = response.json()
    TOKEN_CACHE_PATH.write_text(json.dumps(fresh), encoding="utf-8")
    _token_cache["access_token"] = fresh["access_token"]
    return _token_cache["access_token"]


def _headers(tr_id: str) -> dict:
    return {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {_token()}",
        "appkey": _app_key(),
        "appsecret": _app_secret(),
        "tr_id": tr_id,
        "custtype": "P",
    }


def fetch_live_quote(ticker: str) -> dict:
    """현재가 한 개. price_fetcher.fetch_live_quote()와 같은 반환 형식: {"price":, "as_of":}."""
    _throttle()
    excd = EXCHANGE_BY_TICKER[ticker]
    url = f"{BASE_URL}/uapi/overseas-price/v1/quotations/price"
    params = {"AUTH": "", "EXCD": excd, "SYMB": ticker}
    response = _get_with_retry(url, _headers("HHDFS00000300"), params)
    response.raise_for_status()
    data = response.json()
    if data.get("rt_cd") != "0":
        raise RuntimeError(f"{ticker} 시세 조회 실패: {data}")
    output = data["output"]
    return {"price": float(output["last"]), "as_of": pd.Timestamp.now(tz="UTC")}


def fetch_price_history(ticker: str, start: datetime, end: datetime | None = None) -> pd.Series:
    """일별 종가를 pd.Series(index=date)로 반환. price_fetcher.fetch_price_history()와 동일 인터페이스.

    KIS dailyprice는 한 번에 약 100거래일씩만 주므로, start까지 bymd를 당겨가며 반복 호출한다.
    자주 호출하는 용도가 아니라 (하루 1회 캐시 갱신), 페이지네이션이 조금 느려도 무방하다.
    """
    excd = EXCHANGE_BY_TICKER[ticker]
    end = end or datetime.now()
    rows = []
    bymd = end.strftime("%Y%m%d")

    while True:
        _throttle()
        url = f"{BASE_URL}/uapi/overseas-price/v1/quotations/dailyprice"
        params = {"AUTH": "", "EXCD": excd, "SYMB": ticker, "GUBN": "0", "BYMD": bymd, "MODP": "1"}
        response = _get_with_retry(url, _headers("HHDFS76240000"), params)
        response.raise_for_status()
        data = response.json()
        if data.get("rt_cd") != "0":
            raise RuntimeError(f"{ticker} 기간별시세 조회 실패: {data}")

        page = data.get("output2") or []
        if not page:
            break
        rows.extend(page)

        oldest = min(page, key=lambda r: r["xymd"])["xymd"]
        oldest_date = datetime.strptime(oldest, "%Y%m%d")
        if oldest_date <= start or len(page) < 2:
            break
        bymd = (oldest_date - timedelta(days=1)).strftime("%Y%m%d")

    if not rows:
        raise RuntimeError(f"{ticker}: KIS에서 과거 시세를 하나도 못 받음")

    dates = pd.to_datetime([r["xymd"] for r in rows], format="%Y%m%d")
    closes = pd.to_numeric([r["clos"] for r in rows])
    series = pd.Series(closes, index=dates, name=ticker).sort_index()
    series = series[~series.index.duplicated(keep="last")]
    return series[series.index >= pd.Timestamp(start)]


def fetch_dividend_schedule(sht_cd: str, f_dt: str, t_dt: str) -> list[dict]:
    """예탁원정보(배당일정)[국내주식-145] — 종목의 실제 배당 기준일/주당배당금/지급일을
    그대로 돌려준다(2026-09-20 확인, TR_ID HHKDB669102C0). 계좌별 조회가 아니라 종목
    자체의 실제 배당 데이터라 KIS/키움 계좌 둘 다 이 값으로 실제 배당액을 정확히
    계산할 수 있다 — "연 15% 가정" 어림값 대신 쓴다.

    f_dt/t_dt는 "YYYYMMDD" 형식. 반환: [{"record_date": "YYYY-MM-DD", "per_share": float,
    "pay_date": "YYYY-MM-DD"}, ...] (record_date 오름차순).
    """
    _throttle()
    url = f"{BASE_URL}/uapi/domestic-stock/v1/ksdinfo/dividend"
    params = {"CTS": "", "GB1": "0", "F_DT": f_dt, "T_DT": t_dt, "SHT_CD": sht_cd, "HIGH_GB": ""}
    response = _get_with_retry(url, {**_headers("HHKDB669102C0"), "tr_cont": ""}, params)
    response.raise_for_status()
    data = response.json()
    if data.get("rt_cd") != "0":
        raise RuntimeError(f"{sht_cd} 배당일정 조회 실패: {data}")

    rows = []
    for r in data.get("output1") or []:
        pay_dt = (r.get("divi_pay_dt") or "").replace("/", "-")
        if not pay_dt or not r.get("record_date"):
            continue
        rows.append({
            "record_date": f"{r['record_date'][:4]}-{r['record_date'][4:6]}-{r['record_date'][6:]}",
            "per_share": float(r["per_sto_divi_amt"]),
            "pay_date": pay_dt,
        })
    return sorted(rows, key=lambda r: r["record_date"])


if __name__ == "__main__":
    for t in EXCHANGE_BY_TICKER:
        q = fetch_live_quote(t)
        print(f"{t}: {q['price']} ({q['as_of']})")
