"""키움 해외 **미체결 조회(ust21050)의 실제 응답 필드명**을 확인하는 1회성 probe.

왜 필요한가: auto_trade_loop_kiwoom.py의 추격주문은 "내가 낸 주문의 남은 수량"을 미체결
목록에서 읽어야 하는데, 그 필드명을 아직 실측하지 못했다(확인하려던 시점에 미체결 주문이
하나도 없었음 — 2026-09-18). 지금은 후보 키를 나열해 찾고, 못 찾으면 조용히 넘어가지 않고
예외를 내게 해뒀다. 이 스크립트로 진짜 이름을 확인하면 그 추측을 없앨 수 있다.

하는 일 (모의계좌, 1주):
  1. 현재가를 보고, **절대 체결되지 않을 만큼 낮은 가격**으로 매수 1주를 접수한다.
  2. 미체결 목록을 조회해 그 주문 행의 키 이름을 전부 찍는다.
  3. 바로 취소한다.
미국 정규장이 열려 있어야 한다(호가가 0이면 아무것도 하지 않고 끝낸다).
"""

import json
import sys

from kiwoom_client import (
    cancel_overseas_order,
    get_overseas_orderbook,
    get_overseas_unfilled_orders,
    place_overseas_order,
)

TICKER = "TQQQ"
EXCG = "ND"


def main() -> None:
    book = get_overseas_orderbook(TICKER, EXCG, mode="demo")
    bid = abs(float(book.get("buy_1bid", "0") or "0"))
    if bid <= 0:
        print("호가가 0입니다 — 미국 정규장(22:30~05:00 KST)이 아니라서 확인할 수 없습니다.")
        sys.exit(1)

    safe_price = round(bid * 0.80, 2)  # 20% 아래 — 체결될 리 없다
    print(f"[1/3] 매수 1주 접수 (현재 매수1호가 ${bid:.2f} -> 주문가 ${safe_price:.2f}, 체결 안 되게 낮게)")
    # 장이 닫혀 있어도 호가는 마지막 값이 그대로 내려온다(2026-09-18 실측) — 그래서 위의
    # "호가 0" 검사만으로는 장종료를 못 걸러내고, 주문 단계에서 RC4058로 거절당한다.
    try:
        placed = place_overseas_order(TICKER, "buy", 1, price=safe_price, exchange=EXCG, mode="demo")
    except RuntimeError as exc:
        if "RC4058" in str(exc) or "장종료" in str(exc):
            print("  모의투자 장종료 상태입니다 — 미국 정규장(22:30~05:00 KST)에 다시 실행하세요.")
            sys.exit(1)
        raise
    if placed.get("return_code") != 0:
        print(f"  실패: {placed.get('return_msg')}")
        sys.exit(1)
    ord_no = placed["ord_no"]
    print(f"  접수됨 — 주문번호 {ord_no}")
    print(f"  (주문 응답 키: {sorted(k for k in placed if k not in ('return_code', 'return_msg'))})")

    try:
        print("[2/3] 미체결 목록 조회 — 실제 필드명 확인")
        data = get_overseas_unfilled_orders(stk_cd=TICKER, stex_tp=EXCG, mode="demo")
        rows = data.get("result_list", []) or []
        print(f"  미체결 {len(rows)}건")
        for row in rows:
            print(f"  키 목록: {sorted(row)}")
            print(f"  내용   : {json.dumps(row, ensure_ascii=False)}")
    finally:
        print(f"[3/3] 주문 취소 (주문번호 {ord_no})")
        cancelled = cancel_overseas_order(ord_no, TICKER, exchange=EXCG, mode="demo")
        if cancelled.get("return_code") != 0:
            print(f"  취소 실패: {cancelled.get('return_msg')}")
            print(f"  경고: 계좌에 주문번호 {ord_no}가 남아있을 수 있습니다 — 확인 필요")
            sys.exit(1)
        print("  취소 완료 — 계좌에 흔적 안 남음")


if __name__ == "__main__":
    main()
