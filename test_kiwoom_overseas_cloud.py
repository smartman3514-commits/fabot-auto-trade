"""GitHub Actions(클라우드)에서 키움 **해외(미국)** 모의투자 API가 되는지 확인하는 읽기 전용 테스트.

왜 따로 필요한가: 키움은 모의투자 키가 국내용/해외용으로 완전히 분리되어 있다
(KIWOOM_PAPER_APP_KEY vs KIWOOM_PAPER_OVERSEAS_APP_KEY). 지금까지 워크플로우에는
국내 키만 등록돼 있어서, TQQQ 자동매매를 붙이기 전에 해외 키가 클라우드에서도
발급·조회되는지부터 확인해야 한다.

**주문은 내지 않는다.** 조회만 한다 — 2026-09-18, 1단계.
"""

import sys

from kiwoom_client import (
    abs_price,
    get_overseas_balance,
    get_overseas_cash_balance,
    get_overseas_orderbook,
    get_overseas_quote,
)

TICKER = "TQQQ"


def _check(label: str, fn):
    try:
        data = fn()
    except Exception as exc:
        print(f"  ❌ {label} — 예외: {type(exc).__name__}: {exc}")
        return None
    if data.get("return_code") != 0:
        print(f"  ❌ {label} — {data.get('return_code')} {data.get('return_msg')}")
        return None
    print(f"  ✅ {label}")
    return data


def main() -> None:
    print(f"[1/4] 현재가 조회 ({TICKER})")
    quote = _check("현재가", lambda: get_overseas_quote(TICKER, mode="demo"))
    if quote:
        print(f"      현재가 ${abs_price(quote['cur_prc']):.2f}")

    print(f"[2/4] 10호가 조회 ({TICKER})")
    _check("호가", lambda: get_overseas_orderbook(TICKER, mode="demo"))

    print("[3/4] 해외 예수금 조회")
    cash = _check("예수금", lambda: get_overseas_cash_balance(mode="demo"))
    if cash:
        # 필드명이 문서마다 달라서, 금액처럼 보이는 것만 몇 개 찍어 본다.
        shown = {k: v for k, v in cash.items()
                 if k not in ("return_code", "return_msg") and isinstance(v, str) and v.strip()}
        print(f"      {dict(list(shown.items())[:6])}")

    # stk_cd를 빈 값으로 두면 "종목 코드값이 없습니다[1517]"로 거절당한다(2026-09-18 실측).
    # 문서상으로는 선택 항목처럼 보이지만 실제로는 필수다.
    print("[4/4] 해외 잔고 조회 (ND)")
    bal = _check("잔고", lambda: get_overseas_balance(stk_cd=TICKER, stex_tp="ND", mode="demo"))
    if bal:
        held = next((r for r in bal.get("result_list", []) if r.get("stk_cd") == TICKER), None)
        print(f"      {TICKER} 보유 {int(held['poss_qty'])}주" if held else f"      {TICKER} 미보유")

    if quote is None:
        print("\n결론: 해외 모의 키가 클라우드에서 동작하지 않는다. TQQQ 연결을 진행하면 안 된다.")
        sys.exit(1)
    print("\n결론: 해외 모의 키가 클라우드에서 정상 동작한다. TQQQ 자동실행을 붙여도 된다.")


if __name__ == "__main__":
    main()
