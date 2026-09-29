"""모의계좌는 예수금(현금)에 이자가 안 붙는다. 사용자 요청(2026-09-20)으로 매달 3일에
현금 잔고(원화+달러 환산)에 대해 연 3%를 월할(0.25%)로 계산해서 추정 이자를 기록한다.

원화 예수금과 달러 예수금은 이자가 붙는 통화가 다르다는 지적(2026-09-29)으로, 하나로
합쳐서 계산하던 것을 통화별로 나눠 따로 기록한다.

배당(estimate_dividend.py)과 같은 "현금흐름" 성격이라 대시보드가 이미 action="dividend"로
집계하는 로직(총수익, 계좌 장기 추이 차트)을 그대로 타게 하되, ticker를 CASH_INTEREST_TRADE_KEY_*로
구분해서 표에서는 배당과 별도 줄로 보이게 한다(app.js CASH_INTEREST_TICKERS와 반드시 같은 문자열).
"""
import argparse
import sys

from cooldown import log_trade
from kiwoom_client import get_domestic_cash_balance, get_overseas_cash_balance as kiwoom_get_overseas_cash_balance
from live_order_executor import _inquire_balance_raw
from overseas_order_executor import get_overseas_cash_balance as kis_get_overseas_cash_balance

CASH_INTEREST_TRADE_KEY_KRW = "현금(이자,원화)"  # app.js의 CASH_INTEREST_TICKER_KRW와 동일해야 함
CASH_INTEREST_TRADE_KEY_USD = "현금(이자,달러)"  # app.js의 CASH_INTEREST_TICKER_USD와 동일해야 함
MONTHLY_INTEREST_RATE = 0.03 / 12  # 연 3% 가정을 월할로
TQQQ_TICKER = "TQQQ"
TQQQ_EXCG = "NASD"
# 이자 계산용 원/달러 환율 — app.js의 CASH_RECONSTRUCTION/APPROX_EXCHANGE_RATE와 같은 근사치.
# 정밀한 결제환율이 아니라 "현금에 이자가 얼마나 붙었는지" 추정용이라 고정값으로 충분하다.
APPROX_EXCHANGE_RATE = 1380.3
# 해외 예수금은 T+2~3 결제 지연이 있어 d0(당일)은 아직 정산 전 금액일 수 있다 — 완전히
# 정산된(가장 뒤쪽 필드부터 값이 있는) 금액을 쓴다(broker_live.js kiwoomGetOverseasCashUsd와 동일 로직).
_SETTLED_USD_FIELDS = ["d4_usd_fx_entr", "d3_usd_fx_entr", "d2_usd_fx_entr", "d1_usd_fx_entr", "d0_usd_fx_entr"]


def _kis_cash_parts() -> tuple[float, float]:
    """(원화 예수금, 달러 예수금) — 각각 자기 통화 그대로 반환한다."""
    domestic = float(_inquire_balance_raw()["output2"][0]["dnca_tot_amt"])
    # ref_price는 매수가능금액(cash) 조회 자체엔 영향을 주지 않는다(최대주문수량 계산에만
    # 쓰임) — TQQQ 대략적인 가격대의 아무 양수를 넣어도 된다.
    overseas_usd = kis_get_overseas_cash_balance(TQQQ_TICKER, TQQQ_EXCG, 70.0)
    return domestic, overseas_usd


def _kiwoom_cash_parts() -> tuple[float, float]:
    """(원화 예수금, 달러 예수금) — 각각 자기 통화 그대로 반환한다."""
    domestic = float(get_domestic_cash_balance(mode="demo")["ord_alow_amt"])
    overseas_data = kiwoom_get_overseas_cash_balance(mode="demo")
    settled = next(
        (overseas_data[k] for k in _SETTLED_USD_FIELDS if overseas_data.get(k) not in (None, "")),
        0,
    )
    return domestic, float(settled)


def _log_interest(account: str, ticker: str, currency_label: str, cash_amount: float,
                   krw_amount: float, dry_run: bool = False) -> None:
    amount = round(krw_amount * MONTHLY_INTEREST_RATE)
    if amount <= 0:
        print(f"{account}({currency_label}): 현금 {cash_amount:,.2f} — 이자 대상 없음, 건너뜁니다.")
        return
    if dry_run:
        print(f"{account}({currency_label}): [dry-run] 추정 이자 {amount:,}원 "
              f"(현금 {cash_amount:,.2f} 기준) — 기록 안 함")
        return
    log_trade(
        ticker=ticker,
        action="dividend",
        quantity=1,
        price=amount,
        fg_score=None,
        memo=(f"추정 현금 이자(연 3% 가정 월할, {currency_label} 예수금 {cash_amount:,.2f}×0.25%, "
              f"모의계좌는 실이자가 없어 매월 3일 자동 추정 기록)"),
        account=account,
    )
    print(f"{account}({currency_label}): 추정 이자 {amount:,}원 기록 완료 (현금 {cash_amount:,.2f} 기준)")


def _log_interest_both(account: str, krw_cash: float, usd_cash: float, dry_run: bool = False) -> None:
    _log_interest(account, CASH_INTEREST_TRADE_KEY_KRW, "원화", krw_cash, krw_cash, dry_run=dry_run)
    _log_interest(account, CASH_INTEREST_TRADE_KEY_USD, "달러", usd_cash,
                  usd_cash * APPROX_EXCHANGE_RATE, dry_run=dry_run)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="계산만 하고 실제 기록은 하지 않음")
    args = parser.parse_args()

    ok = True

    try:
        krw_cash, usd_cash = _kis_cash_parts()
        _log_interest_both("KIS 모의투자", krw_cash, usd_cash, dry_run=args.dry_run)
    except Exception as exc:
        ok = False
        print(f"KIS 이자 추정 실패: {exc}")

    try:
        krw_cash, usd_cash = _kiwoom_cash_parts()
        _log_interest_both("키움 모의투자", krw_cash, usd_cash, dry_run=args.dry_run)
    except Exception as exc:
        ok = False
        print(f"Kiwoom 이자 추정 실패: {exc}")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
