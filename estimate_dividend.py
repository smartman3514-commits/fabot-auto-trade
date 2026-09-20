"""472150(TIGER 배당커버드콜액티브)의 실제 배당은 KIS 예탁원정보(배당일정) API로 정확히
조회할 수 있다(2026-09-20 확인 — record_date/주당배당금/지급일이 실제 값 그대로 나옴,
kis_price_client.fetch_dividend_schedule 참고). 그래서 "연 15% 가정 월할" 어림값 대신,
이 실제 배당률과 그 기준일(record_date) 시점의 실제 보유수량을 곱해서 정확한 배당액을
계산해 기록한다.

이 API는 계좌별이 아니라 종목 자체의 실제 배당 데이터라 KIS/키움 두 계좌 모두 이 값으로
계산한다 — 실제로 계좌 현금에 반영됐는지 여부와 무관하게 기록해야 한다: 대시보드의
"총수익"은 krwProfit(보유종목 매입가 대비 평가손익, 현금과 무관)에 이 기록된 배당 누적을
더해서 계산하므로, 기록하지 않으면 실제로 받은 배당이 총수익에서 빠진다. (2026-09-19에
"KIS는 실제로 배당이 현금에 반영된다"는 걸 확인하고 이중 계상을 걱정해서 KIS 기록을
잠깐 껐었는데, krwProfit이 애초에 현금을 안 보는 값이라 이중 계상이 아니었다 — 다시 켠다,
2026-09-20.)

기록은 실제 지급일(pay_date)로 남긴다 — 이 스크립트를 실행한 날짜가 아니라. 같은
계좌·같은 지급일 기록이 이미 있으면 다시 기록하지 않는다(중복 방지, 매달 5일 실행 중에
과거분도 같이 훑기 때문에 안전하게 여러 번 돌려도 됨).
"""
import sys
from datetime import date

import requests

from cooldown import _load_journal_env
from kis_price_client import fetch_dividend_schedule

COVERED_CALL_STOCK_CODE = "472150"
COVERED_CALL_TRADE_KEY = "TIGER 배당커버드콜액티브(472150)"
ACCOUNTS = ["KIS 모의투자", "키움 모의투자"]
# 계좌 시작(KIS 07-23)보다 여유 있게 이른 조회 시작일 — 놓친 과거분도 같이 훑는다.
SCHEDULE_LOOKBACK_START = "20260701"


def _sb_headers(env: dict) -> dict:
    return {
        "apikey": env["SUPABASE_SERVICE_KEY"],
        "Authorization": f"Bearer {env['SUPABASE_SERVICE_KEY']}",
    }


def _get_buy_trades(env: dict, account: str) -> list[dict]:
    url = f"{env['SUPABASE_URL']}/rest/v1/trades"
    params = {
        "select": "trade_date,quantity",
        "ticker": f"eq.{COVERED_CALL_TRADE_KEY}",
        "action": "eq.buy",
        "account": f"eq.{account}",
        "order": "trade_date.asc",
    }
    resp = requests.get(url, params=params, headers=_sb_headers(env), timeout=10)
    resp.raise_for_status()
    return resp.json()


def _shares_held_at(buy_trades: list[dict], as_of_date: str) -> float:
    return sum(float(t["quantity"]) for t in buy_trades if t["trade_date"] <= as_of_date)


def _existing_dividend_pay_dates(env: dict, account: str) -> set:
    url = f"{env['SUPABASE_URL']}/rest/v1/trades"
    params = {
        "select": "trade_date",
        "ticker": f"eq.{COVERED_CALL_TRADE_KEY}",
        "action": "eq.dividend",
        "account": f"eq.{account}",
    }
    resp = requests.get(url, params=params, headers=_sb_headers(env), timeout=10)
    resp.raise_for_status()
    return {row["trade_date"] for row in resp.json()}


def _log_dividend(env: dict, account: str, pay_date: str, amount: int, memo: str) -> None:
    url = f"{env['SUPABASE_URL']}/rest/v1/trades"
    body = {
        "trade_date": pay_date,
        "ticker": COVERED_CALL_TRADE_KEY,
        "action": "dividend",
        "quantity": 1,
        "price": amount,
        "fg_score": None,
        "memo": memo,
        "account": account,
    }
    headers = {**_sb_headers(env), "Content-Type": "application/json", "Prefer": "return=minimal"}
    resp = requests.post(url, json=body, headers=headers, timeout=10)
    resp.raise_for_status()


def main() -> int:
    env = _load_journal_env()
    schedule = fetch_dividend_schedule(
        COVERED_CALL_STOCK_CODE, SCHEDULE_LOOKBACK_START, date.today().strftime("%Y%m%d")
    )
    if not schedule:
        print("배당 일정 조회 결과가 없습니다.")
        return 0

    ok = True
    for account in ACCOUNTS:
        try:
            buy_trades = _get_buy_trades(env, account)
            if not buy_trades:
                print(f"{account}: 472150 매수 기록 없음 — 건너뜁니다.")
                continue
            already = _existing_dividend_pay_dates(env, account)
            for div in schedule:
                if div["pay_date"] in already:
                    continue
                shares = _shares_held_at(buy_trades, div["record_date"])
                if shares <= 0:
                    continue  # 기준일 이후에 처음 매수한 계좌는 그 배당 대상이 아님
                amount = round(shares * div["per_share"])
                if amount <= 0:
                    continue
                memo = (f"실제 배당(예탁원 배당일정 API, 기준일 {div['record_date']} 주당 "
                        f"{div['per_share']:,.0f}원 × 그날 보유 {shares:,.0f}주, 지급일 {div['pay_date']})")
                _log_dividend(env, account, div["pay_date"], amount, memo)
                print(f"{account}: {div['pay_date']} 배당 {amount:,}원 기록 완료 "
                      f"(기준일 {div['record_date']}, {shares:,.0f}주 × {div['per_share']:,.0f}원)")
        except Exception as exc:
            ok = False
            print(f"{account} 배당 처리 실패: {exc}")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
