"""오늘의 F&G 신호를 판정하고, 커버드콜 추가매수 조건이면 키움증권 모의투자 계좌에서
바로 매수까지 진행한다. auto_trade_loop.py(KIS)의 키움판 — 계좌가 완전히 별개라서
쿨다운/매매기록도 독립적으로 관리한다(account="키움 모의투자").

**2026-08-14 업데이트 — KIS와 같은 방식(추격주문)으로 전환**: 처음엔 "한 번 주문 넣고
몇 초 기다렸다가 늘어난 만큼만 체결로 인정"하는 단순한 방식으로 시작했는데, 실제로
4,821주를 주문했더니 8초 안에는 31주만 체결되고 나머지는 그 이후 서서히 다 체결된
걸 확인했다(2026-08-14 실측 — 매매기록에 31주로 잘못 남아서 나중에 4,821주로 정정).
그래서 미체결내역(ka10075, "oso" 목록)의 실제 필드명을 테스트 주문으로 직접 확인한
뒤(ord_no/oso_qty), KIS 국내판과 같은 "가격 밀리면 정정, 시간/횟수 넘으면 취소"
추격주문 상태 머신을 만들었다. 실시간 웹소켓이 없어서 REST 폴링 방식(해외판과 같은
아이디어)으로 가격 변화를 감지한다.

TQQQ(해외)는 이번 범위에서 뺐다 — 키움 모의투자 해외 잔고 조회(ust21070)가 종목코드
없이는 "계좌 전체" 조회가 안 되는 걸 확인해서(2026-08-14), 국내 커버드콜만 다룬다.

필요 환경변수: KIWOOM_PAPER_APP_KEY, KIWOOM_PAPER_APP_SECRET
필요 파일: ../fabot-trade-journal/.env (SUPABASE_URL, SUPABASE_SERVICE_KEY)

사용법:
    python auto_trade_loop_kiwoom.py           # 신호 판정 + (해당되면) 실제 주문 실행
    python auto_trade_loop_kiwoom.py --dry-run # 신호만 판정하고 주문은 절대 넣지 않음
"""

import argparse
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

import account_summary
import today_signal
import voice_briefing
from cooldown import log_trade
from kiwoom_client import (
    amend_domestic_order,
    cancel_domestic_order,
    get_domestic_cash_balance,
    get_domestic_holdings,
    get_domestic_orderbook,
    get_domestic_unfilled_orders,
    place_domestic_order,
)

COVERED_CALL_STOCK_CODE = "472150"
COVERED_CALL_TRADE_KEY = "TIGER 배당커버드콜액티브(472150)"
# auto_trade_loop.py와 같은 이유로 today_signal.COVERED_CALL_ALLOCATION 하나로 통일
# (2026-08-22) — 라벨 텍스트와 실제 배분 비율이 따로 놀지 않게.
COVERED_CALL_ALLOCATION = today_signal.COVERED_CALL_ALLOCATION

KIWOOM_ACCOUNT_LABEL = "키움 모의투자"


def _not_executed(note: str) -> dict:
    return {"executed": False, "note": note}


def _current_holding() -> dict | None:
    data = get_domestic_holdings(mode="demo")
    for h in data.get("acnt_evlt_remn_indv_tot", []):
        if h["stk_cd"].lstrip("A") == COVERED_CALL_STOCK_CODE:
            return {"qty": int(h["rmnd_qty"]), "avg_price": float(h["pur_pric"])}
    return None


def _sweep_price(book: dict, side: str, qty: int) -> int:
    """호가창에서 남은 수량을 다 받아줄 만큼 충분히 깊은 가격을 계산한다
    (KIS/해외판과 같은 아이디어 — 최우선호가 잔량만 보고 걸면 부족할 수 있음)."""
    prefix = "sel" if side == "buy" else "buy"  # buy면 매도호가를 쓸어담고, sell이면 매수호가를 쓸어담음
    levels = [("fpr", f"{prefix}_fpr_bid", f"{prefix}_fpr_req")] + [
        (str(n), f"{prefix}_{n}th_pre_bid", f"{prefix}_{n}th_pre_req") for n in range(2, 11)
    ]
    cumulative = 0
    last_price = 0
    for _, price_key, qty_key in levels:
        price = int(book.get(price_key, "0") or "0")
        level_qty = int(book.get(qty_key, "0") or "0")
        if price <= 0:
            continue
        last_price = price
        cumulative += level_qty
        if cumulative >= qty:
            return price
    if last_price <= 0:
        raise RuntimeError("호가가 전부 0입니다 — 국내 정규장 시간(09:00~15:30 KST)이 아닐 가능성이 높습니다.")
    return last_price


def _compute_qty(cash: int, ref_price: int) -> int:
    budget = cash * COVERED_CALL_ALLOCATION
    return int(budget // ref_price)


class KiwoomChaseOrder:
    """REST 폴링으로 가격을 지켜보며 체결될 때까지 정정을 반복하는 상태 머신
    (live_order_executor.ChaseOrder/overseas_order_executor.OverseasChaseOrder와 같은 아이디어,
    키움 API 필드명에 맞게 구현 — ord_no/oso_qty는 2026-08-14 테스트 주문으로 실측 확인함)."""

    def __init__(self, stk_cd: str, side: str, total_qty: int,
                 max_reprices: int, max_seconds: float, poll_interval: float):
        self.stk_cd = stk_cd
        self.side = side
        self.total_qty = total_qty
        self.max_reprices = max_reprices
        self.max_seconds = max_seconds
        self.poll_interval = poll_interval

        self.filled_qty = 0
        self.reprice_count = 0
        self.order = None  # {"ord_no":, "price":}
        self.done = False
        self.started_at = time.monotonic()

    def _remaining(self) -> int:
        return self.total_qty - self.filled_qty

    def _remaining_from_unfilled(self) -> int | None:
        data = get_domestic_unfilled_orders(stk_cd=self.stk_cd, mode="demo")
        match = next((r for r in data.get("oso", []) if r["ord_no"] == self.order["ord_no"]), None)
        return int(match["oso_qty"]) if match else None  # None = 미체결 목록에 없음 = 전량 체결(혹은 취소/거부)

    def _refresh_fill(self) -> None:
        if self.order is None:
            return
        remaining = self._remaining_from_unfilled()
        self.filled_qty = self.total_qty if remaining is None else self.total_qty - remaining
        print(f"  체결 확인: {self.filled_qty}/{self.total_qty}주")
        if self._remaining() <= 0:
            self.done = True

    def _place_or_reprice(self) -> None:
        if self.order is not None:
            self._refresh_fill()  # 정정 직전 레이스 컨디션 방지(KIS/해외판과 동일한 이유)
            if self.done:
                return

        book = get_domestic_orderbook(self.stk_cd, mode="demo")
        price = _sweep_price(book, self.side, self._remaining())

        if self.order is None:
            result = place_domestic_order(self.stk_cd, self.side, self._remaining(), price=price, mode="demo")
            if result.get("return_code") != 0:
                raise RuntimeError(f"주문 실패: {result.get('return_msg')}")
            self.order = {"ord_no": result["ord_no"], "price": price}
            print(f"  주문 접수: {self.side} {self._remaining()}주 @ {price}원 (주문번호 {result['ord_no']})")
            return

        if self.order["price"] == price:
            return

        print(f"  가격 이동 감지 — 정정주문 ({self.order['price']}원 -> {price}원)")
        try:
            result = amend_domestic_order(self.order["ord_no"], self.stk_cd, qty=self._remaining(), price=price, mode="demo")
            if result.get("return_code") != 0:
                raise RuntimeError(result.get("return_msg", "알 수 없는 오류"))
        except Exception as exc:  # noqa: BLE001
            self._refresh_fill()
            if self.done:
                return
            raise RuntimeError(f"정정 실패했는데 아직 미체결 잔량이 남아있음: {exc}") from exc
        self.reprice_count += 1
        self.order = {"ord_no": result["ord_no"], "price": price}
        print(f"  정정 완료: {self.side} @ {price}원 (주문번호 {result['ord_no']})")

    def _give_up(self) -> None:
        if self.order is None:
            return
        try:
            result = cancel_domestic_order(self.order["ord_no"], self.stk_cd, qty=0, mode="demo")
            if result.get("return_code") != 0:
                raise RuntimeError(result.get("return_msg", "알 수 없는 오류"))
            print(f"  남은 미체결 주문(주문번호 {self.order['ord_no']})을 취소했습니다.")
        except Exception as exc:  # noqa: BLE001
            self._refresh_fill()
            if not self.done:
                print(f"  경고: 중단 시 취소 실패 — 미체결 주문이 그대로 남아있을 수 있음: {exc}")

    def run(self) -> None:
        self._place_or_reprice()
        self._refresh_fill()

        while not self.done:
            if time.monotonic() - self.started_at > self.max_seconds:
                print(f"  최대 실행 시간({self.max_seconds}초) 초과 — 중단")
                self._give_up()
                return
            if self.reprice_count >= self.max_reprices:
                print(f"  최대 재주문 횟수({self.max_reprices}) 초과 — 중단 (미체결 {self._remaining()}주 남음)")
                self._give_up()
                return

            time.sleep(self.poll_interval)

            book = get_domestic_orderbook(self.stk_cd, mode="demo")
            tick_key = "sel_fpr_bid" if self.side == "buy" else "buy_fpr_bid"
            tick_price = int(book.get(tick_key, "0") or "0")
            stale = (
                tick_price > 0
                and ((self.side == "buy" and tick_price > self.order["price"])
                     or (self.side == "sell" and tick_price < self.order["price"]))
            )
            if stale:
                self._place_or_reprice()
            else:
                self._refresh_fill()


def execute_covered_call_buy(today_info: dict, dry_run: bool) -> dict:
    cash_data = get_domestic_cash_balance(mode="demo")
    cash = int(cash_data["ord_alow_amt"])

    book = get_domestic_orderbook(COVERED_CALL_STOCK_CODE, mode="demo")
    ref_price = int(book.get("sel_fpr_bid", "0") or "0")
    if ref_price <= 0:
        print("호가가 0입니다 — 국내 정규장 시간(09:00~15:30 KST)이 아니라서 실행할 수 없습니다.")
        return _not_executed("국내 정규장 시간이 아니라 호가를 받을 수 없어 실행하지 않았습니다")

    qty = _compute_qty(cash, ref_price)
    print(f"주문가능금액 {cash:,}원 -> {COVERED_CALL_ALLOCATION:.0%} 배분, 주문수량 {qty}주 ({COVERED_CALL_STOCK_CODE})")

    if qty <= 0:
        print("계산된 수량이 0주라 주문을 생략합니다.")
        return _not_executed("주문가능 수량이 0주라 실행하지 않았습니다")
    if dry_run:
        print("--dry-run 모드 — 실제 주문은 넣지 않음.")
        return _not_executed("dry-run 모드라 실제 주문은 넣지 않았습니다")

    qty_before = _current_holding()
    # 스케줄 실행 창(15:19~15:30 KST, 장 마감 직전) 동안은 계속 쫓아가도 되게 여유 있게 잡음
    chaser = KiwoomChaseOrder(COVERED_CALL_STOCK_CODE, "buy", qty, max_reprices=60, max_seconds=630.0, poll_interval=3.0)
    chaser.run()

    if not chaser.done:
        print(f"미완료 종료: {chaser.filled_qty}/{qty}주만 체결됨 — 매매기록은 실제 체결분만 남김.")
        if chaser.filled_qty <= 0:
            return _not_executed("주문이 체결되지 않았습니다")

    holding = _current_holding()
    if holding is None:
        print("경고: 체결 후 보유내역 조회에서 해당 종목을 못 찾음 — 매매기록을 남기지 못했습니다.")
        return _not_executed("체결 후 보유내역 조회에 실패했습니다")

    log_trade(
        ticker=COVERED_CALL_TRADE_KEY,
        action="buy",
        quantity=chaser.filled_qty,
        price=holding["avg_price"],
        fg_score=today_info["score"],
        memo=f"자동실행(auto_trade_loop_kiwoom.py), 신호상 명목종목='{today_signal.COVERED_CALL_TICKER}'",
        account=KIWOOM_ACCOUNT_LABEL,
    )
    print(f"매매기록 저장 완료: buy {chaser.filled_qty}주 @ {holding['avg_price']}원 (ticker='{COVERED_CALL_TRADE_KEY}')")
    return {"executed": True, "action": "buy", "ticker": COVERED_CALL_TRADE_KEY,
             "qty": chaser.filled_qty, "price": holding["avg_price"]}


def main() -> None:
    parser = argparse.ArgumentParser(description="오늘의 F&G 신호를 판정하고 실행 가능하면 키움 모의계좌에서 바로 매수까지 진행한다.")
    parser.add_argument("--dry-run", action="store_true", help="신호만 판정하고 실제 주문은 넣지 않음")
    parser.add_argument(
        "--realtime", action="store_true",
        help="전일 확정 종가 대신 지금 이 순간의 실시간 계산값을 쓴다 (마감 직전 스케줄 실행용)",
    )
    args = parser.parse_args()

    today_info = today_signal.get_cnn_score() if args.realtime else today_signal.get_today_score()
    raw = today_signal.judge_raw_signal(today_info["score"])
    cooldown_key = COVERED_CALL_TRADE_KEY if raw.action == "buy_covered_call" else None
    result = today_signal.apply_cooldown(raw, cooldown_key=cooldown_key, account=KIWOOM_ACCOUNT_LABEL)
    today_signal.log_result(today_info, raw, result)

    print("=== [키움] 오늘의 F&G 신호 ===")
    print(f"날짜: {today_info['date']}" + (" (실시간 조회 실패 — 마지막 캐시값 사용)" if today_info["stale"] else ""))
    try:
        print(account_summary.format_composition_line(account_summary.get_account_composition("Kiwoom")))
    except Exception as exc:
        print(f"(계좌 구성 조회 실패 — {exc})")
    print(f"F&G {today_info['score']:.0f}점으로 {today_info.get('zone', today_info['rating'])}입니다.")
    print(f"원 판정: {raw.label}")
    print(f"최종 신호: {result['final_label']}")

    outcome = _not_executed("실행 대상 액션이 아니었습니다")

    if raw.action == "wait":
        print("-> 대기. 실행 없음.")
    elif result["cooldown"] and result["cooldown"]["in_cooldown"]:
        print(f"-> 쿨다운 중이라 실행 안 함 ({result['cooldown']['reason']})")
    elif raw.action == "buy_covered_call":
        outcome = execute_covered_call_buy(today_info, args.dry_run)
    elif raw.action in ("buy_tqqq", "sell_tqqq"):
        print("-> TQQQ 신호이지만 이 스크립트(키움판)는 아직 해외주식 자동실행을 지원하지 않음.")
    else:
        print(f"-> 알 수 없는 액션({raw.action}) — 실행 안 함.")

    text, audio_path = voice_briefing.synthesize_briefing(today_info, raw, result, outcome)
    print(f"\n음성 브리핑: {text}")
    print(f"음성 파일 저장: {audio_path}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n중지했습니다.")
