"""demo_balance/demo_cash(Supabase, fabot-trade-journal 소유 표)에서 계좌별 총 자산과
구성 비율(현금/커버드콜/TQQQ)을 가져온다 — 텔레그램 리포트에 "총 자산 ...원(현금70.9%,
Tiger배당커버드콜29.1%, TQQQ0%)" 줄을 넣기 위함(인선님 요청, 2026-08-21).

cooldown.py의 trades 테이블과는 다른 표(demo_balance/demo_cash)를 보고, broker 컬럼값도
다르다("KIS"/"Kiwoom" — cooldown.py가 쓰는 account="KIS 모의투자"/"키움 모의투자"와 별개).
"""

import requests

from cooldown import _load_journal_env

COVERED_CALL_TICKER_CODE = "472150"
COVERED_CALL_LABEL = "Tiger배당커버드콜"
TQQQ_TICKER = "TQQQ"


def get_account_composition(broker: str) -> dict:
    """broker: demo_balance/demo_cash의 broker 컬럼 값 그대로("KIS" 또는 "Kiwoom")."""
    env = _load_journal_env()
    headers = {
        "apikey": env["SUPABASE_SERVICE_KEY"],
        "Authorization": f"Bearer {env['SUPABASE_SERVICE_KEY']}",
    }
    base = env["SUPABASE_URL"]

    balance_resp = requests.get(
        f"{base}/rest/v1/demo_balance", headers=headers,
        params={"broker": f"eq.{broker}", "select": "ticker,krw_current_value"},
        timeout=10,
    )
    balance_resp.raise_for_status()
    rows = balance_resp.json()

    cash_resp = requests.get(
        f"{base}/rest/v1/demo_cash", headers=headers,
        params={"broker": f"eq.{broker}", "select": "krw_amount"},
        timeout=10,
    )
    cash_resp.raise_for_status()
    cash_rows = cash_resp.json()
    cash_krw = float(cash_rows[0]["krw_amount"]) if cash_rows else 0.0

    covered_call_krw = sum(float(r["krw_current_value"]) for r in rows if r["ticker"] == COVERED_CALL_TICKER_CODE)
    tqqq_krw = sum(float(r["krw_current_value"]) for r in rows if r["ticker"] == TQQQ_TICKER)
    total = cash_krw + covered_call_krw + tqqq_krw

    def pct(v: float) -> float:
        return (v / total * 100) if total > 0 else 0.0

    return {
        "total_krw": total,
        "cash_pct": pct(cash_krw),
        "covered_call_pct": pct(covered_call_krw),
        "tqqq_pct": pct(tqqq_krw),
    }


def format_composition_line(comp: dict) -> str:
    return (
        f"총 자산 {comp['total_krw']:,.0f}원"
        f"(현금 {comp['cash_pct']:.1f}%, {COVERED_CALL_LABEL} {comp['covered_call_pct']:.1f}%, "
        f"{TQQQ_TICKER} {comp['tqqq_pct']:.1f}%)"
    )
