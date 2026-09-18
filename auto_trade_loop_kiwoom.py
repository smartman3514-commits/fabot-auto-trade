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

**2026-09-18 업데이트 — TQQQ(해외) 자동실행 연결**: 2026-08-14에는 해외 잔고 조회(ust21070)가
"계좌 전체" 조회를 지원하지 않는다는 이유로 TQQQ를 범위에서 뺐었다. 그런데 실제 원인은
stk_cd(종목코드)가 **선택이 아니라 필수**였던 것이고, 종목코드를 넣으면 정상 조회된다
(2026-09-18 실측). 그 사이 KIS 모의는 TQQQ를 매수하는데 키움 모의는 신호만 찍고 넘어가는
상태가 계속됐다 — 2026-09-18 04:45 KST F&G 29점 매수 신호 때 사용자가 발견.

응답 필드는 전부 실측으로 확인한 것만 쓴다(2026-09-18):
  - 호가(usa20101)  : sel_1bid~sel_10bid / sel_1bid_req~, buy_1bid~ / buy_1bid_req~
  - 예수금(ust21160): d0_usd_fx_entr 가 주문가능 달러
  - 잔고(ust21070)  : result_list[].poss_qty(보유수량), .frgn_stk_book_uv(평균단가)
미체결(ust21050)의 필드명만은 조회 시점에 미체결 주문이 하나도 없어 확인하지 못했다.
후보 키로 찾아보고 못 알아보면 **경고를 찍고 보유수량 증감으로 체결량을 대신 잰다** —
처음엔 여기서 예외를 내게 했는데, 그러면 "세는 방법을 모른다"는 이유로 매수 자체가
통째로 막힌다. F&G≤30 매수 신호는 자주 오지 않아서 한 번 놓치면 회복이 안 되므로,
주문을 넣는 쪽을 우선한다(2026-09-18, 사용자가 실제 누락을 겪고 지적함).
정확한 필드명은 probe_kiwoom_overseas_unfilled.py로 장중에 확인한다.

필요 환경변수: KIWOOM_PAPER_APP_KEY, KIWOOM_PAPER_APP_SECRET (국내)
              KIWOOM_PAPER_OVERSEAS_APP_KEY, KIWOOM_PAPER_OVERSEAS_APP_SECRET (해외)
필요 파일: ../fabot-trade-journal/.env (SUPABASE_URL, SUPABASE_SERVICE_KEY)

사용법:
    python auto_trade_loop_kiwoom.py           # 신호 판정 + (해당되면) 실제 주문 실행
    python auto_trade_loop_kiwoom.py --dry-run # 신호만 판정하고 주문은 절대 넣지 않음
"""

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.stdout.reconfigure(encoding="utf-8")

import account_summary
import today_signal
import voice_briefing
from cooldown import log_trade
from kiwoom_client import (
    amend_domestic_order,
    amend_overseas_order,
    cancel_domestic_order,
    cancel_overseas_order,
    get_domestic_cash_balance,
    get_domestic_holdings,
    get_domestic_orderbook,
    get_domestic_unfilled_orders,
    get_overseas_balance,
    get_overseas_cash_balance,
    get_overseas_orderbook,
    get_overseas_unfilled_orders,
    place_domestic_order,
    place_overseas_order,
)

COVERED_CALL_STOCK_CODE = "472150"
COVERED_CALL_TRADE_KEY = "TIGER 배당커버드콜액티브(472150)"
# auto_trade_loop.py와 같은 이유로 today_signal.COVERED_CALL_ALLOCATION 하나로 통일
# (2026-08-22) — 라벨 텍스트와 실제 배분 비율이 따로 놀지 않게.
COVERED_CALL_ALLOCATION = today_signal.COVERED_CALL_ALLOCATION

KIWOOM_ACCOUNT_LABEL = "키움 모의투자"

TQQQ_TICKER = today_signal.TQQQ_TICKER
TQQQ_EXCG = "ND"  # 키움 거래소 구분(나스닥) — KIS의 "NASD"와 표기가 다르다

# 키움 해외에는 "최대 주문가능 수량" 조회가 없어서(2026-09-18 확인) 수수료 몫을 이 비율로
# 직접 떼어 둔다. KIS가 같은 시점에 보여준 실측 차이(우리 계산 1,048주 vs 한도 1,037주,
# 약 1.07%)를 근거로 잡은 **추정치**다 — 키움 한도 조회를 찾으면 그 값으로 대체할 것.
OVERSEAS_FEE_MARGIN = 0.011

# 임계값은 절대 여기 적지 않고 today_signal 하나만 본다. 같은 상수를 두 파일에 따로 들고
# 있다가 2026-08-31 임계값 변경이 한쪽에만 반영되어, F&G 26~30 구간에서 신호는 정확히
# 잡히고도 배분 계산에서 ValueError로 조용히 실패해 실제 매수가 누락된 사고가 있었다
# (2026-09-17 발견, auto_trade_loop.py의 같은 주석 참고). 그 사고를 여기서 반복하지 않는다.
_t100, _t50, _t25 = today_signal.BUY_THRESHOLDS
_TQQQ_BUY_ALLOCATION_BY_SCORE = [(_t100, 1.0), (_t50, 0.5), (_t25, 0.25)]
_s50, _s100 = today_signal.SELL_THRESHOLDS
_TQQQ_SELL_FRACTION_BY_SCORE = [(_s100, 1.0), (_s50, 0.5)]


def _tqqq_buy_allocation(score: float) -> float:
    for threshold, fraction in _TQQQ_BUY_ALLOCATION_BY_SCORE:
        if score <= threshold:
            return fraction
    raise ValueError(f"매수 신호가 아닌 점수({score})로 배분 비율을 계산하려고 함")


def _tqqq_sell_fraction(score: float) -> float:
    for threshold, fraction in sorted(_TQQQ_SELL_FRACTION_BY_SCORE, reverse=True):
        if score >= threshold:
            return fraction
    raise ValueError(f"매도 신호가 아닌 점수({score})로 매도 비율을 계산하려고 함")


KST = timezone(timedelta(hours=9))
# auto_trade_loop.py(KIS)와 같은 이유(2026-08-31) — 국내장이 열려 있는 시간대(09:00~15:30)라면
# 호가가 정상적으로 잡혀서 기존의 "호가 0 = 정규장 아님" 체크만으로는 15:19~15:30 종가 실행
# 창을 벗어난 시각(예: 2026-08-28 12:53 KST 실제 사례)의 매수를 막지 못한다.
DOMESTIC_CLOSE_WINDOW_KST = ((15, 15), (15, 35))


def _within_domestic_close_window(now: datetime | None = None) -> bool:
    now = (now or datetime.now(timezone.utc)).astimezone(KST)
    (start_h, start_m), (end_h, end_m) = DOMESTIC_CLOSE_WINDOW_KST
    start = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    return start <= now <= end


# TQQQ(나스닥) 마감은 항상 16:00 America/New_York — 서머타임 계산은 zoneinfo에 맡긴다
# (auto_trade_loop.py와 같은 이유·같은 값, 2026-08-31).
NYSE_TZ = ZoneInfo("America/New_York")
TQQQ_WINDOW_BEFORE = timedelta(minutes=20)
TQQQ_WINDOW_AFTER = timedelta(minutes=20)


def _today_nyse_close_kst(now_utc: datetime) -> datetime:
    close_local = now_utc.astimezone(NYSE_TZ).replace(hour=16, minute=0, second=0, microsecond=0)
    return close_local.astimezone(KST)


def _within_tqqq_close_window(now: datetime | None = None) -> bool:
    now_utc = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)
    close_kst = _today_nyse_close_kst(now_utc)
    return (close_kst - TQQQ_WINDOW_BEFORE
            <= now_utc.astimezone(KST)
            <= close_kst + TQQQ_WINDOW_AFTER)


def _tqqq_window_message(now: datetime | None = None) -> str:
    now_utc = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)
    close_kst = _today_nyse_close_kst(now_utc)
    return (f"실행 창(오늘 마감 {close_kst.strftime('%H:%M')} KST 전후 20분) 밖 "
            f"(현재 {now_utc.astimezone(KST).strftime('%H:%M')})")


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


# ── TQQQ(해외) ──────────────────────────────────────────────────────────────

def _overseas_usd_cash() -> float:
    """주문가능 달러. d0_usd_fx_entr = 당일 결제기준 외화예수금(2026-09-18 실측으로 확인)."""
    data = get_overseas_cash_balance(mode="demo")
    return float(data.get("d0_usd_fx_entr", "0") or "0")


def _tqqq_holding() -> dict | None:
    data = get_overseas_balance(stk_cd=TQQQ_TICKER, stex_tp=TQQQ_EXCG, mode="demo")
    for h in data.get("result_list", []):
        if h.get("stk_cd") == TQQQ_TICKER:
            return {"qty": int(h["poss_qty"]), "avg_price": float(h["frgn_stk_book_uv"])}
    return None


def _overseas_sweep_price(book: dict, side: str, qty: int) -> float:
    """국내판 _sweep_price와 같은 아이디어 — 남은 수량을 다 받아줄 만큼 깊은 가격.
    해외는 필드명이 sel_1bid/sel_1bid_req 형식이고 가격에 부호(+/-)가 붙는다."""
    prefix = "sel" if side == "buy" else "buy"
    cumulative = 0
    last_price = 0.0
    for n in range(1, 11):
        price = abs(float(book.get(f"{prefix}_{n}bid", "0") or "0"))
        level_qty = int(book.get(f"{prefix}_{n}bid_req", "0") or "0")
        if price <= 0:
            continue
        last_price = price
        cumulative += level_qty
        if cumulative >= qty:
            return price
    if last_price <= 0:
        raise RuntimeError("호가가 전부 0입니다 — 미국 정규장 시간(22:30~05:00 KST)이 아닐 가능성이 높습니다.")
    return last_price


class KiwoomOverseasChaseOrder:
    """국내판 KiwoomChaseOrder의 해외 버전. 상태 머신 구조는 같고 API만 /api/us/* 를 쓴다."""

    # 미체결(ust21050) 응답 필드명을 아직 실측하지 못해(조회 시 미체결 주문이 없었음)
    # 후보를 나열해 두고 찾는다. 하나도 못 찾으면 예외를 내서 실제 키를 드러낸다.
    _ORD_NO_KEYS = ("ord_no", "orig_ord_no", "ordr_no")
    _REMAIN_KEYS = ("oso_qty", "rmn_qty", "unfl_qty", "ord_rmnq")
    _UNKNOWN = object()  # "필드명을 못 알아봤다" — None(전량 체결)과 구분해야 한다

    def __init__(self, side: str, total_qty: int,
                 max_reprices: int, max_seconds: float, poll_interval: float,
                 qty_before: int = 0):
        self._qty_before = qty_before
        self._use_holding_fallback = False
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
        """미체결 잔량. 필드명을 못 알아보면 _UNKNOWN을 돌려주고, 호출부가 보유수량
        차이로 대체 측정한다 — **여기서 예외를 내서 매수 자체를 막지는 않는다.**
        신호가 자주 오지 않는데 '체결 수량을 세는 방법'이 틀렸다는 이유로 매수를
        통째로 놓치는 것이 훨씬 큰 손해이기 때문이다(2026-09-18 사용자 지적)."""
        try:
            data = get_overseas_unfilled_orders(stk_cd=TQQQ_TICKER, stex_tp=TQQQ_EXCG, mode="demo")
        except Exception as exc:
            print(f"  경고: 미체결 조회 API 실패({exc}) — 보유수량 변화로 대신 셉니다.")
            return self._UNKNOWN
        rows = data.get("result_list", []) or []
        for row in rows:
            ord_no = next((row[k] for k in self._ORD_NO_KEYS if k in row), None)
            remain = next((row[k] for k in self._REMAIN_KEYS if k in row), None)
            if ord_no is None or remain is None:
                print(f"  경고: 미체결 응답 필드명을 못 알아봤습니다 — 보유수량 변화로 "
                      f"대신 셉니다. 실제 키 목록: {sorted(row)}")
                return self._UNKNOWN
            if str(ord_no) != str(self.order["ord_no"]):
                continue
            return int(remain)
        return None  # 목록에 없음 = 전량 체결(혹은 취소/거부)

    def _filled_from_holding(self) -> int:
        """보유수량 증감으로 체결량을 잰다(대체 수단). 정산 반영이 늦으면 실제보다
        적게 나올 수 있어서, 한 번 올라간 값은 내려가지 않게 max로만 갱신한다."""
        holding = _tqqq_holding()
        now_qty = holding["qty"] if holding else 0
        moved = (now_qty - self._qty_before) if self.side == "buy" else (self._qty_before - now_qty)
        return max(0, min(self.total_qty, moved))

    def _refresh_fill(self) -> None:
        if self.order is None:
            return
        remaining = self._remaining_from_unfilled()
        if remaining is self._UNKNOWN:
            self._use_holding_fallback = True
        if self._use_holding_fallback:
            self.filled_qty = max(self.filled_qty, self._filled_from_holding())
        else:
            self.filled_qty = self.total_qty if remaining is None else self.total_qty - remaining
        print(f"  체결 확인: {self.filled_qty}/{self.total_qty}주"
              + (" (보유수량 기준)" if self._use_holding_fallback else ""))
        if self._remaining() <= 0:
            self.done = True

    def _place_or_reprice(self) -> None:
        if self.order is not None:
            self._refresh_fill()
            if self.done:
                return

        book = get_overseas_orderbook(TQQQ_TICKER, mode="demo")
        price = round(_overseas_sweep_price(book, self.side, self._remaining()), 2)

        if self.order is None:
            result = place_overseas_order(TQQQ_TICKER, self.side, self._remaining(),
                                          price=price, exchange=TQQQ_EXCG, mode="demo")
            if result.get("return_code") != 0:
                raise RuntimeError(f"주문 실패: {result.get('return_msg')}")
            self.order = {"ord_no": result["ord_no"], "price": price}
            print(f"  주문 접수: {self.side} {self._remaining()}주 @ ${price} (주문번호 {result['ord_no']})")
            return

        if self.order["price"] == price:
            return

        print(f"  가격 이동 감지 — 정정주문 (${self.order['price']} -> ${price})")
        try:
            result = amend_overseas_order(self.order["ord_no"], TQQQ_TICKER, price=price,
                                          exchange=TQQQ_EXCG, mode="demo")
            if result.get("return_code") != 0:
                raise RuntimeError(result.get("return_msg", "알 수 없는 오류"))
        except Exception as exc:  # noqa: BLE001
            self._refresh_fill()
            if self.done:
                return
            raise RuntimeError(f"정정 실패했는데 아직 미체결 잔량이 남아있음: {exc}") from exc
        self.reprice_count += 1
        self.order = {"ord_no": result["ord_no"], "price": price}
        print(f"  정정 완료: {self.side} @ ${price} (주문번호 {result['ord_no']})")

    def _give_up(self) -> None:
        if self.order is None:
            return
        try:
            result = cancel_overseas_order(self.order["ord_no"], TQQQ_TICKER,
                                           exchange=TQQQ_EXCG, mode="demo")
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

            book = get_overseas_orderbook(TQQQ_TICKER, mode="demo")
            tick_key = "sel_1bid" if self.side == "buy" else "buy_1bid"
            tick_price = abs(float(book.get(tick_key, "0") or "0"))
            stale = (
                tick_price > 0
                and ((self.side == "buy" and tick_price > self.order["price"])
                     or (self.side == "sell" and tick_price < self.order["price"]))
            )
            if stale:
                self._place_or_reprice()
            else:
                self._refresh_fill()


def _tqqq_preflight(today_info: dict, ignore_window: bool = False) -> tuple[float, float] | dict:
    """실행 창·호가 확인. 통과하면 (기준가, 주문가능달러), 아니면 _not_executed(...).

    ignore_window: 마감 실행 창 가드를 이번 실행에 한해 건너뛴다. 기본 원칙은 "마감에
    산다"이지만, 놓친 신호를 장중에 따라가야 할 때 사용자가 명시적으로 켜는 용도다
    (2026-09-18: 키움이 TQQQ를 아예 실행하지 않아 신호를 놓친 건을 당일 장중에 복구).
    자동 스케줄에는 절대 기본으로 켜지 않는다.
    """
    if ignore_window:
        print("⚠ --ignore-window: 마감 실행 창 가드를 건너뜁니다(사용자가 명시적으로 지정).")
    elif not _within_tqqq_close_window():
        msg = _tqqq_window_message()
        print(f"지금은 {msg} — 건너뜁니다.")
        return _not_executed(f"{msg}이라 실행하지 않았습니다")

    book = get_overseas_orderbook(TQQQ_TICKER, mode="demo")
    ref_price = abs(float(book.get("sel_1bid", "0") or "0"))
    if ref_price <= 0:
        print("호가가 전부 0입니다 — 미국 정규장 시간(22:30~05:00 KST)이 아니라서 실행할 수 없습니다.")
        return _not_executed("미국 정규장 시간이 아니라 호가를 받을 수 없어 실행하지 않았습니다")
    return ref_price, _overseas_usd_cash()


def execute_tqqq_buy(today_info: dict, dry_run: bool, ignore_window: bool = False) -> dict:
    pre = _tqqq_preflight(today_info, ignore_window)
    if isinstance(pre, dict):
        return pre
    ref_price, cash = pre

    allocation = _tqqq_buy_allocation(today_info["score"])
    # 수수료 몫을 빼고 계산한다. KIS는 증권사가 직접 한도(max_ord_psbl_qty)를 알려줘서
    # 그 값으로 자르지만(auto_trade_loop.py), 키움 해외에는 대응되는 조회가 없다
    # (2026-09-18 확인). KIS가 실측으로 보여준 차이가 약 1.07%였으므로 같은 폭을 여유로
    # 둔다 — 100% 단계에서 실탄을 전부 쓰면 수수료만큼 모자라 주문이 거부되기 때문이다.
    # 키움에 한도 조회 API가 확인되면 이 추정치 대신 그 값을 쓸 것.
    budget = cash * allocation * (1 - OVERSEAS_FEE_MARGIN)
    qty = int(budget // ref_price)
    print(f"주문가능 외화현금 ${cash:,.2f} -> {allocation:.0%} 배분"
          f"(수수료 여유 {OVERSEAS_FEE_MARGIN:.1%} 제외), 주문수량 {qty}주 ({TQQQ_TICKER})")

    if qty <= 0:
        print("계산된 수량이 0주라 주문을 생략합니다.")
        return _not_executed("주문가능 수량이 0주라 실행하지 않았습니다")
    if dry_run:
        print("--dry-run 모드 — 실제 주문은 넣지 않음.")
        return _not_executed("dry-run 모드라 실제 주문은 넣지 않았습니다")

    before = _tqqq_holding()
    chaser = KiwoomOverseasChaseOrder("buy", qty, max_reprices=15, max_seconds=300.0,
                                      poll_interval=3.0,
                                      qty_before=before["qty"] if before else 0)
    chaser.run()

    if not chaser.done:
        print(f"미완료 종료: {chaser.filled_qty}/{qty}주만 체결됨 — 매매기록은 실제 체결분만 남김.")
        if chaser.filled_qty <= 0:
            return _not_executed("주문이 체결되지 않았습니다")

    holding = _tqqq_holding()
    if holding is None:
        print("경고: 체결 후 보유내역 조회에서 해당 종목을 못 찾음 — 매매기록을 남기지 못했습니다.")
        return _not_executed("체결 후 보유내역 조회에 실패했습니다")

    log_trade(
        ticker=TQQQ_TICKER,
        action="buy",
        quantity=chaser.filled_qty,
        price=holding["avg_price"],
        fg_score=today_info["score"],
        memo="자동실행(auto_trade_loop_kiwoom.py), 해외주식(TQQQ) REST 폴링 추격주문",
        account=KIWOOM_ACCOUNT_LABEL,
    )
    print(f"매매기록 저장 완료: buy {chaser.filled_qty}주 @ ${holding['avg_price']} (ticker='{TQQQ_TICKER}')")
    return {"executed": True, "action": "buy", "ticker": TQQQ_TICKER,
            "qty": chaser.filled_qty, "price": holding["avg_price"]}


def execute_tqqq_sell(today_info: dict, dry_run: bool, ignore_window: bool = False) -> dict:
    pre = _tqqq_preflight(today_info, ignore_window)
    if isinstance(pre, dict):
        return pre

    holding = _tqqq_holding()
    if holding is None or holding["qty"] <= 0:
        print(f"보유 중인 {TQQQ_TICKER}가 없어 매도할 수 없습니다.")
        return _not_executed(f"보유 중인 {TQQQ_TICKER}가 없어 실행하지 않았습니다")

    fraction = _tqqq_sell_fraction(today_info["score"])
    qty = int(holding["qty"] * fraction)
    print(f"보유 {holding['qty']}주 -> {fraction:.0%} 매도, 주문수량 {qty}주 ({TQQQ_TICKER})")

    if qty <= 0:
        print("계산된 수량이 0주라 주문을 생략합니다.")
        return _not_executed("매도 수량이 0주라 실행하지 않았습니다")
    if dry_run:
        print("--dry-run 모드 — 실제 주문은 넣지 않음.")
        return _not_executed("dry-run 모드라 실제 주문은 넣지 않았습니다")

    avg_before = holding["avg_price"]  # 매도 후에는 평균단가가 사라질 수 있어 미리 잡아둔다
    chaser = KiwoomOverseasChaseOrder("sell", qty, max_reprices=15, max_seconds=300.0,
                                      poll_interval=3.0, qty_before=holding["qty"])
    chaser.run()

    if not chaser.done:
        print(f"미완료 종료: {chaser.filled_qty}/{qty}주만 체결됨 — 매매기록은 실제 체결분만 남김.")
        if chaser.filled_qty <= 0:
            return _not_executed("주문이 체결되지 않았습니다")

    log_trade(
        ticker=TQQQ_TICKER,
        action="sell",
        quantity=chaser.filled_qty,
        price=avg_before,
        fg_score=today_info["score"],
        memo="자동실행(auto_trade_loop_kiwoom.py), 해외주식(TQQQ) REST 폴링 추격주문 (가격은 매도 전 평균단가)",
        account=KIWOOM_ACCOUNT_LABEL,
    )
    print(f"매매기록 저장 완료: sell {chaser.filled_qty}주 (ticker='{TQQQ_TICKER}')")
    return {"executed": True, "action": "sell", "ticker": TQQQ_TICKER,
            "qty": chaser.filled_qty, "price": avg_before}


# ── 커버드콜(국내) ───────────────────────────────────────────────────────────

def execute_covered_call_buy(today_info: dict, dry_run: bool) -> dict:
    if not _within_domestic_close_window():
        now_kst = datetime.now(timezone.utc).astimezone(KST).strftime("%H:%M")
        (sh, sm), (eh, em) = DOMESTIC_CLOSE_WINDOW_KST
        print(f"지금은 {now_kst} KST — 국내장 종가 실행 창({sh:02d}:{sm:02d}~{eh:02d}:{em:02d})이 아니라 건너뜁니다.")
        return _not_executed(f"실행 창({sh:02d}:{sm:02d}~{eh:02d}:{em:02d} KST) 밖이라 실행하지 않았습니다 (현재 {now_kst})")

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
    parser.add_argument(
        "--ignore-window", action="store_true",
        help="TQQQ 마감 실행 창 가드를 건너뛴다. 놓친 신호를 장중에 따라갈 때만 쓴다 — "
             "자동 스케줄에는 절대 기본으로 켜지 말 것",
    )
    args = parser.parse_args()

    today_info = today_signal.get_cnn_score() if args.realtime else today_signal.get_today_score()
    raw = today_signal.judge_raw_signal(today_info["score"])
    cooldown_key = COVERED_CALL_TRADE_KEY if raw.action == "buy_covered_call" else None
    result = today_signal.apply_cooldown(raw, cooldown_key=cooldown_key, account=KIWOOM_ACCOUNT_LABEL,
                                         score=today_info["score"])
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
    elif raw.action == "buy_tqqq":
        outcome = execute_tqqq_buy(today_info, args.dry_run, args.ignore_window)
    elif raw.action == "sell_tqqq":
        outcome = execute_tqqq_sell(today_info, args.dry_run, args.ignore_window)
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
