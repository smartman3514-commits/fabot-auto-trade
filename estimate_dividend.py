"""모의계좌는 실제 배당이 안 나온다(2026-09-19 실측 확인 — 08-14 매수 후 5주가 지나도
현금이 그대로였음). 그래서 매달 5일에 이 스크립트를 돌려서, 대시보드의 "예상 월 현금흐름"과
같은 가정(연 15%, app.js 참고)으로 커버드콜(472150) 평가금액의 1.25%를 추정 배당으로
trades 테이블에 기록한다. 사용자 지정(2026-09-19): KIS/키움 두 계좌 다 기록.

[2026-09-20 수정] KIS는 위 가정과 달리 실제로 배당이 들어오는 것으로 확인됨 — 09-05
추정 배당(3,014,281원) 기록 이후, 실제 HTS 예수금이 "시작 현금 - 매수에 쓴 돈"보다
정확히 그 금액만큼 더 많았다(환율 변수가 없는 원화 예수금만으로 확인, 오차 없음 —
사용자가 eFriend 모의투자 계좌 잔고 화면 직접 확인). 그래서 KIS는 이 스크립트가 추정
배당을 또 기록하면 이중 계상이 된다 — KIS는 건너뛰고 키움만 기록한다(키움은 09-19
기준 5주 이상 현금 변동 없음을 실측 확인함, 계속 추정 필요).
"""
import sys

from cooldown import log_trade
from kiwoom_client import get_domestic_holdings

COVERED_CALL_STOCK_CODE = "472150"
COVERED_CALL_TRADE_KEY = "TIGER 배당커버드콜액티브(472150)"
MONTHLY_DIVIDEND_RATE = 0.15 / 12  # 연 15% 가정을 월할로 — app.js의 0.0125와 동일


def _kiwoom_eval_amount() -> float | None:
    data = get_domestic_holdings(mode="demo")
    for h in data.get("acnt_evlt_remn_indv_tot", []):
        if h["stk_cd"].lstrip("A") == COVERED_CALL_STOCK_CODE:
            return float(h["evlt_amt"])
    return None


def _log_dividend(account: str, eval_amount: float) -> None:
    amount = round(eval_amount * MONTHLY_DIVIDEND_RATE)
    log_trade(
        ticker=COVERED_CALL_TRADE_KEY,
        action="dividend",
        quantity=1,
        price=amount,
        fg_score=None,
        memo=(f"추정 배당(연 15% 가정 월할, 평가금액 {eval_amount:,.0f}원×1.25%, "
              f"모의계좌는 실배당이 없어 매월 5일 자동 추정 기록)"),
        account=account,
    )
    print(f"{account}: 추정 배당 {amount:,}원 기록 완료 (평가금액 {eval_amount:,.0f}원 기준)")


def main() -> int:
    ok = True

    # KIS는 실제 배당이 들어오는 것으로 확인돼(2026-09-20) 더 이상 추정 기록하지 않는다 —
    # 위 모듈 docstring 참고. 실제 배당 자체는 broker_live.js가 조회하는 실제 현금에 이미
    # 반영되므로, 대시보드 총수익 계산에서 빠지지 않는다.
    print("KIS: 실제 배당이 들어오는 것으로 확인되어 추정 기록을 건너뜁니다.")

    try:
        kiwoom_amount = _kiwoom_eval_amount()
        if kiwoom_amount is None:
            print("Kiwoom: 472150 보유 없음 — 건너뜁니다.")
        else:
            _log_dividend("키움 모의투자", kiwoom_amount)
    except Exception as exc:
        ok = False
        print(f"Kiwoom 배당 추정 실패: {exc}")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
