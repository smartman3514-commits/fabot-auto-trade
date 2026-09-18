"""해외주식(TQQQ 등) 지정가 주문을 끝까지 쫓아가 체결시킨다 — live_order_executor.py의 해외판.

`live_order_executor.py`(국내주식)와 목적은 같지만 메커니즘이 다르다: 모의투자에서는 해외주식
실시간 웹소켓 시세가 아예 지원되지 않으므로(2026-07-22 공식 문서로 확인, HDFSASP0/HDFSCNT0
모의투자 미지원) "가격이 밀렸는지"를 웹소켓 틱이 아니라 REST 호가 조회를 주기적으로 폴링해서
판단한다. 정정은 국내와 마찬가지로 취소+재주문 대신 정정주문(RVSE_CNCL_DVSN_CD=01) 1회 호출로
처리한다(단, 해외주식 정정은 국내와 달리 '잔량전부' 편의 플래그가 없어 ORD_QTY에 정확한 남은
수량을 넣어야 함).

**중요 — 아직 실전(모의투자) 주문으로 검증되지 않음**: 이 스크립트를 작성한 시점(2026-07-23 낮,
한국시간)은 미국 정규장 시간(22:30~05:00 KST, 서머타임 기준)이 아니라서 호가창이 전부 0으로
비어 있어 실제 주문 제출까지는 테스트하지 못했다. 정규장 시간에 실제로 한 번 돌려서 확인할 것.
(장시작전에 주문하면 rt_cd=1, msg_cd=40570000, msg1="모의투자 장시작전입니다"로 거부됨 — 이건
버그가 아니라 정상적인 시장시간 게이트임, 이전에도 확인된 사실.)

필요 환경변수: KIS_PAPER_APP_KEY, KIS_PAPER_APP_SECRET, KIS_PAPER_STOCK

사용법:
    python overseas_order_executor.py <종목코드> <buy|sell> <수량> [--excg NASD] [--max-reprices N] [--max-seconds N] [--poll-interval N]

예:
    python overseas_order_executor.py TQQQ buy 10
"""

import argparse
import json
import time

import requests

from live_order_executor import BASE_URL, _headers, _cano, ACNT_PRDT_CD, _throttle, _raise_verbose, _get_with_retry

QUOTE_TR_ID = "HHDFS76200100"  # 해외주식 현재가 1호가 (모의/실전 공통)

# 주문/계좌 API는 4자리 거래소코드(NASD/NYSE/AMEX)를 쓰는데, 시세(호가) API는 3자리 코드(NAS/NYS/AMS)를
# 따로 씀 — 실전에서 EXCD="NASD"로 호출하면 "ERROR INVALID FID_COND_MRKT_DIV_CODE"로 실패하는 걸 확인함
# (2026-07-23). 두 코드 체계를 혼동하지 않도록 여기서 명시적으로 매핑한다.
_QUOTE_EXCD_BY_ORDER_EXCG = {"NASD": "NAS", "NYSE": "NYS", "AMEX": "AMS"}


def _get_asking_price(pdno: str, excg: str) -> dict:
    """호가 10단(pbid1..10/pask1..10, vbid.../vask...) 조회."""
    quote_excd = _QUOTE_EXCD_BY_ORDER_EXCG.get(excg, excg)
    _throttle()
    response = _get_with_retry(
        f"{BASE_URL}/uapi/overseas-price/v1/quotations/inquire-asking-price",
        headers={**_headers(QUOTE_TR_ID), "tr_cont": ""},
        params={"AUTH": "", "EXCD": quote_excd, "SYMB": pdno},
    )
    _raise_verbose(response)
    data = response.json()
    if data["rt_cd"] != "0":
        raise RuntimeError(f"호가 조회 실패: {data['msg1']}")
    return data["output2"]  # 단일 객체(리스트 아님) — MCP 래퍼가 pd.DataFrame([...])로 감싸는 것과 다름


def _compute_sweep_price(book: dict, side: str, qty: int) -> float:
    """남은 수량을 다 받아줄 만큼 충분히 깊은 가격을 호가창에서 계산한다 (국내판과 동일한 아이디어)."""
    levels = range(1, 11)
    cumulative = 0
    if side == "buy":
        for level in levels:
            cumulative += int(float(book[f"vask{level}"]))
            price = float(book[f"pask{level}"])
            if price <= 0:
                continue  # 호가 없음(장 마감 등)
            if cumulative >= qty:
                return price
        price = float(book["pask10"])
    else:
        for level in levels:
            cumulative += int(float(book[f"vbid{level}"]))
            price = float(book[f"pbid{level}"])
            if price <= 0:
                continue
            if cumulative >= qty:
                return price
        price = float(book["pbid10"])
    if price <= 0:
        raise RuntimeError("호가가 전부 0입니다 — 장시간 외(폐장 중)일 가능성이 높습니다.")
    return price


def _place_order(pdno: str, excg: str, side: str, qty: int, price: float) -> dict:
    tr_id = ("VTTT1002U" if side == "buy" else "VTTT1001U")
    body = {
        "CANO": _cano(), "ACNT_PRDT_CD": ACNT_PRDT_CD, "OVRS_EXCG_CD": excg, "PDNO": pdno,
        "ORD_QTY": str(qty), "OVRS_ORD_UNPR": f"{price:.2f}",
        "CTAC_TLNO": "", "MGCO_APTM_ODNO": "", "SLL_TYPE": "00" if side == "sell" else "",
        "ORD_SVR_DVSN_CD": "0", "ORD_DVSN": "00",
    }
    headers = {**_headers(tr_id), "tr_cont": "", "Accept": "text/plain", "charset": "UTF-8"}
    _throttle()
    response = requests.post(
        f"{BASE_URL}/uapi/overseas-stock/v1/trading/order",
        headers=headers, data=json.dumps(body), timeout=20,
    )
    _raise_verbose(response)
    data = response.json()
    if data["rt_cd"] != "0":
        raise RuntimeError(f"주문 실패: {data['msg1']}")
    return data["output"]


def _amend_order(pdno: str, excg: str, orgn_odno: str, qty: int, price: float) -> dict:
    body = {
        "CANO": _cano(), "ACNT_PRDT_CD": ACNT_PRDT_CD, "OVRS_EXCG_CD": excg, "PDNO": pdno,
        "ORGN_ODNO": orgn_odno, "RVSE_CNCL_DVSN_CD": "01",
        "ORD_QTY": str(qty), "OVRS_ORD_UNPR": f"{price:.2f}",
        "MGCO_APTM_ODNO": "", "ORD_SVR_DVSN_CD": "0",
    }
    headers = {**_headers("VTTT1004U"), "tr_cont": "", "Accept": "text/plain", "charset": "UTF-8"}
    _throttle()
    response = requests.post(
        f"{BASE_URL}/uapi/overseas-stock/v1/trading/order-rvsecncl",
        headers=headers, data=json.dumps(body), timeout=20,
    )
    _raise_verbose(response)
    data = response.json()
    if data["rt_cd"] != "0":
        raise RuntimeError(f"정정 실패: {data['msg1']}")
    return data["output"]


def _cancel_order(pdno: str, excg: str, orgn_odno: str, qty: int) -> dict:
    body = {
        "CANO": _cano(), "ACNT_PRDT_CD": ACNT_PRDT_CD, "OVRS_EXCG_CD": excg, "PDNO": pdno,
        "ORGN_ODNO": orgn_odno, "RVSE_CNCL_DVSN_CD": "02",
        "ORD_QTY": str(qty), "OVRS_ORD_UNPR": "0",
        "MGCO_APTM_ODNO": "", "ORD_SVR_DVSN_CD": "0",
    }
    headers = {**_headers("VTTT1004U"), "tr_cont": "", "Accept": "text/plain", "charset": "UTF-8"}
    _throttle()
    response = requests.post(
        f"{BASE_URL}/uapi/overseas-stock/v1/trading/order-rvsecncl",
        headers=headers, data=json.dumps(body), timeout=20,
    )
    _raise_verbose(response)
    data = response.json()
    if data["rt_cd"] != "0":
        raise RuntimeError(f"취소 실패: {data['msg1']}")
    return data["output"]


def _check_remaining(pdno: str, excg: str, odno: str) -> int | None:
    """미체결내역(inquire_nccs)에서 이 주문을 찾는다. 없으면 전량 체결(또는 거부/취소)된 것으로 본다."""
    params = {
        "CANO": _cano(), "ACNT_PRDT_CD": ACNT_PRDT_CD, "OVRS_EXCG_CD": excg,
        "SORT_SQN": "DS", "CTX_AREA_FK200": "", "CTX_AREA_NK200": "",
    }
    _throttle()
    response = _get_with_retry(
        f"{BASE_URL}/uapi/overseas-stock/v1/trading/inquire-nccs",
        headers={**_headers("VTTS3018R"), "tr_cont": ""}, params=params,
    )
    _raise_verbose(response)
    data = response.json()
    if data["rt_cd"] != "0":
        raise RuntimeError(f"미체결내역 조회 실패: {data['msg1']}")
    match = next((r for r in data["output"] if r["odno"] == odno and r["pdno"] == pdno), None)
    if match is None:
        return None  # 전량 체결(혹은 이미 취소/거부)됨
    return int(match["nccs_qty"])


def get_overseas_order_capacity(pdno: str, excg: str, ref_price: float) -> dict:
    """주문가능 외화현금과 **증권사가 직접 계산한 최대 주문가능 수량**을 함께 돌려준다.

    왜 max_qty까지 받아오는가(2026-09-18): 우리 계산은 `현금 // 기준가`인데, KIS는 수수료
    몫을 빼고 한도를 잡는다. 실측으로 $74,725.98 / $71.30 이면 우리는 1,048주인데 KIS가
    말하는 한도는 1,037주였다(약 1.07% 차이). 25%·50% 단계에서는 여유가 많아 드러나지
    않지만, **F&G<=20의 100% 단계에서는 실탄을 다 쓰므로 한도를 넘겨 주문이 거부된다** —
    하필 가장 중요한 매수에서 터지는 종류의 문제다. 그래서 둘 중 작은 값을 쓴다.
    """
    _throttle()
    response = _get_with_retry(
        f"{BASE_URL}/uapi/overseas-stock/v1/trading/inquire-psamount",
        headers={**_headers("VTTS3007R"), "tr_cont": ""},
        params={
            "CANO": _cano(), "ACNT_PRDT_CD": ACNT_PRDT_CD, "OVRS_EXCG_CD": excg,
            "OVRS_ORD_UNPR": f"{ref_price:.2f}", "ITEM_CD": pdno,
        },
    )
    _raise_verbose(response)
    data = response.json()
    if data["rt_cd"] != "0":
        raise RuntimeError(f"매수가능금액 조회 실패: {data['msg1']}")
    out = data["output"]
    raw_max = out.get("max_ord_psbl_qty")
    return {
        "cash": float(out["ord_psbl_frcr_amt"]),
        # 필드가 없거나 비어 오면 한도 없음(None)으로 두고, 호출부가 기존처럼 동작하게 한다.
        "max_qty": int(raw_max) if str(raw_max or "").strip().isdigit() else None,
    }


def get_overseas_cash_balance(pdno: str, excg: str, ref_price: float) -> float:
    """주문가능 외화현금(USD, ord_psbl_frcr_amt)만 필요할 때 쓰는 얇은 래퍼."""
    return get_overseas_order_capacity(pdno, excg, ref_price)["cash"]


def _get_overseas_cash_balance_legacy(pdno: str, excg: str, ref_price: float) -> float:
    """(옛 구현 — get_overseas_order_capacity로 대체됨)"""
    _throttle()
    response = _get_with_retry(
        f"{BASE_URL}/uapi/overseas-stock/v1/trading/inquire-psamount",
        headers={**_headers("VTTS3007R"), "tr_cont": ""},
        params={
            "CANO": _cano(), "ACNT_PRDT_CD": ACNT_PRDT_CD, "OVRS_EXCG_CD": excg,
            "OVRS_ORD_UNPR": f"{ref_price:.2f}", "ITEM_CD": pdno,
        },
    )
    _raise_verbose(response)
    data = response.json()
    if data["rt_cd"] != "0":
        raise RuntimeError(f"매수가능금액 조회 실패: {data['msg1']}")
    return float(data["output"]["ord_psbl_frcr_amt"])


def get_overseas_holding(pdno: str, excg: str) -> dict | None:
    """보유 종목의 수량/평균단가 조회(없으면 None). 체결 후 매매기록 로깅과 매도 수량 계산에 씀."""
    _throttle()
    response = _get_with_retry(
        f"{BASE_URL}/uapi/overseas-stock/v1/trading/inquire-balance",
        headers={**_headers("VTTS3012R"), "tr_cont": ""},
        params={
            "CANO": _cano(), "ACNT_PRDT_CD": ACNT_PRDT_CD, "OVRS_EXCG_CD": excg,
            "TR_CRCY_CD": "USD", "CTX_AREA_FK200": "", "CTX_AREA_NK200": "",
        },
    )
    _raise_verbose(response)
    data = response.json()
    if data["rt_cd"] != "0":
        raise RuntimeError(f"해외잔고 조회 실패: {data['msg1']}")
    match = next((r for r in data["output1"] if r["ovrs_pdno"] == pdno), None)
    if match is None:
        return None
    return {"qty": int(match["ovrs_cblc_qty"]), "avg_price": float(match["pchs_avg_pric"])}


class OverseasChaseOrder:
    """REST 폴링으로 가격을 지켜보며 체결될 때까지 정정을 반복하는 상태 머신 (해외주식판)."""

    def __init__(self, pdno: str, excg: str, side: str, total_qty: int,
                 max_reprices: int, max_seconds: float, poll_interval: float):
        self.pdno = pdno
        self.excg = excg
        self.side = side
        self.total_qty = total_qty
        self.max_reprices = max_reprices
        self.max_seconds = max_seconds
        self.poll_interval = poll_interval

        self.filled_qty = 0
        self.reprice_count = 0
        self.order = None  # {"odno":, "price":, "qty":}
        self.done = False
        self.started_at = time.monotonic()

    def _remaining(self) -> int:
        return self.total_qty - self.filled_qty

    def _refresh_fill(self) -> None:
        if self.order is None:
            return
        remaining = _check_remaining(self.pdno, self.excg, self.order["odno"])
        if remaining is None:
            self.filled_qty = self.total_qty
        else:
            self.filled_qty = self.total_qty - remaining
        print(f"  체결 확인: {self.filled_qty}/{self.total_qty}주")
        if self._remaining() <= 0:
            self.done = True

    def _place_or_reprice(self) -> None:
        if self.order is not None:
            self._refresh_fill()  # 정정 직전 레이스 컨디션 방지 (국내판과 동일한 이유)
            if self.done:
                return

        book = _get_asking_price(self.pdno, self.excg)
        price = _compute_sweep_price(book, self.side, self._remaining())

        if self.order is None:
            result = _place_order(self.pdno, self.excg, self.side, self._remaining(), price)
            self.order = {"odno": result["ODNO"], "price": price}
            print(f"  주문 접수: {self.side} {self._remaining()}주 @ {price}달러 (주문번호 {result['ODNO']})")
            return

        if self.order["price"] == price:
            return

        print(f"  가격 이동 감지 — 정정주문 ({self.order['price']}달러 -> {price}달러)")
        try:
            result = _amend_order(self.pdno, self.excg, self.order["odno"], self._remaining(), price)
        except RuntimeError as exc:
            self._refresh_fill()
            if self.done:
                return
            raise RuntimeError(f"정정 실패했는데 아직 미체결 잔량이 남아있음: {exc}") from exc
        self.reprice_count += 1
        self.order = {"odno": result["ODNO"], "price": price}
        print(f"  정정 완료: {self.side} @ {price}달러 (주문번호 {result['ODNO']})")

    def _give_up(self) -> None:
        if self.order is None:
            return
        try:
            _cancel_order(self.pdno, self.excg, self.order["odno"], self._remaining())
            print(f"  남은 미체결 주문(주문번호 {self.order['odno']})을 취소했습니다.")
        except RuntimeError as exc:
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

            book = _get_asking_price(self.pdno, self.excg)
            tick_price = float(book["pask1"] if self.side == "buy" else book["pbid1"])
            stale = (
                tick_price > 0
                and ((self.side == "buy" and tick_price > self.order["price"])
                     or (self.side == "sell" and tick_price < self.order["price"]))
            )
            if stale:
                self._place_or_reprice()
            else:
                self._refresh_fill()


def main() -> None:
    parser = argparse.ArgumentParser(description="REST 폴링으로 해외주식 지정가 주문을 체결시킨다 (모의투자).")
    parser.add_argument("stock_code")
    parser.add_argument("side", choices=["buy", "sell"])
    parser.add_argument("qty", type=int)
    parser.add_argument("--excg", default="NASD", help="해외거래소코드 (기본값 NASD — TQQQ 등 나스닥 종목)")
    parser.add_argument("--max-reprices", type=int, default=15)
    parser.add_argument("--max-seconds", type=float, default=300.0)
    parser.add_argument("--poll-interval", type=float, default=3.0, help="호가 재확인 주기(초) — 웹소켓이 없어 폴링함")
    args = parser.parse_args()

    print(
        f"{args.side} {args.qty}주 {args.stock_code}({args.excg}) — REST 폴링 추격 주문 시작 "
        f"(최대 {args.max_seconds}초, 재주문 {args.max_reprices}회, 폴링 간격 {args.poll_interval}초)"
    )
    chaser = OverseasChaseOrder(args.stock_code, args.excg, args.side, args.qty,
                                 args.max_reprices, args.max_seconds, args.poll_interval)
    chaser.run()

    if chaser.done:
        print(f"전량 체결 완료: {chaser.filled_qty}/{chaser.total_qty}주 (재주문 {chaser.reprice_count}회)")
    else:
        print(f"미완료 종료: {chaser.filled_qty}/{chaser.total_qty}주 체결, {chaser._remaining()}주 미체결")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n중지했습니다.")
