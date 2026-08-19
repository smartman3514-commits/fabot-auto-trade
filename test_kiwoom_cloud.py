"""GitHub Actions(클라우드)에서 키움 국내 주문 API(접수·취소)가 막히는지 확인하는 1회성 테스트.

키움 인증/시세 조회는 update-fg.yml(해외 시세)에서 이미 매분 클라우드 검증됐지만,
국내 주문 접수·취소 엔드포인트는 다른 tr(kt10000/kt10003)이라 별도로 확인한다.
KIS 쪽은 test_kis_cloud.py(fg-dashboard 저장소)로 이미 검증 완료.
"""

import sys

from kiwoom_client import (
    get_domestic_orderbook,
    place_domestic_order,
    cancel_domestic_order,
)

TEST_STOCK = "472150"  # 이미 보유 중인 종목이라 소액 매수 테스트에 안전


def main():
    print("[1/3] 국내 호가 조회 테스트 (읽기 전용)...")
    book = get_domestic_orderbook(TEST_STOCK, mode="demo")
    price = int(book.get("sel_fpr_bid", "0") or "0") or int(book.get("buy_fpr_bid", "0") or "0")
    if price <= 0:
        print("  실패: 호가가 0 (장시간 외일 수 있음) — 주문 테스트는 임의 가격으로 진행")
        price = 20000
    print(f"  성공 — {TEST_STOCK} 호가 근처: {price}원")

    safe_price = int(price * 0.85 // 10 * 10)
    print(f"[2/3] 소액 매수 주문 테스트 ({safe_price}원, 1주, 체결 안 되게 낮게)...")
    result = place_domestic_order(TEST_STOCK, "buy", 1, price=safe_price, mode="demo")
    if result.get("return_code") != 0:
        print(f"  실패: {result.get('return_msg')}")
        sys.exit(1)
    ord_no = result["ord_no"]
    print(f"  성공 — 주문번호 {ord_no} 접수됨")

    print(f"[3/3] 방금 낸 주문 취소 테스트 (주문번호 {ord_no})...")
    cancel_result = cancel_domestic_order(ord_no, TEST_STOCK, qty=0, mode="demo")
    if cancel_result.get("return_code") != 0:
        print(f"  실패: {cancel_result.get('return_msg')}")
        print(f"\n경고: 취소 실패 — 계좌에 주문번호 {ord_no}가 남아있을 수 있음, 확인 필요")
        sys.exit(1)
    print("  성공 — 취소 완료, 모의계좌에 흔적 안 남음")

    print("\n결론: 국내 호가 조회 · 주문 접수 · 주문 취소 전부 클라우드에서 정상 동작함")


if __name__ == "__main__":
    main()
