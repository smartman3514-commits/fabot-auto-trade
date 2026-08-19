"""실시간 호가 스트림(H0STCNT0)을 보면서 국내주식 지정가 주문을 끝까지 쫓아가 체결시킨다.

오늘 수동으로 반복했던 패턴을 자동화한 것: 가격이 움직여서 미체결로 쌓이면
직접 취소하고 새 호가에 맞춰 재주문하는 과정을, 실시간 웹소켓 틱을 트리거로 삼아
자동으로 반복한다.

- 가격 결정(주문 낼 때/재주문할 때)은 매번 `inquire_asking_price_exp_ccn`(호가 10단)을
  REST로 새로 조회해서, 남은 수량을 다 받아줄 만큼 충분히 깊은 가격을 계산한다
  (2026-07-23 실전에서 확인: 최우선호가 잔량만 보고 걸면 부족할 수 있음).
- 실시간 웹소켓(H0STCNT0)은 "지금 우리 주문가가 더 이상 시장가를 못 따라가고 있는지"를
  틱마다 감시하는 저지연 트리거로만 쓴다 — 매번 REST를 폴링하는 것보다 반응이 빠르다.
- 체결 여부는 `inquire_daily_ccld`로 확인한다(`inquire_ccnl`은 시세용 체결 데이터라 다른 API임 —
  2026-07-23에 이미 한 번 헷갈렸던 부분).

모의투자 전용으로 작성됨(VTTC0012U/VTTC0011U/VTTC0013U/VTTC0081R tr_id). 실전 전환 시
tr_id와 BASE_URL을 바꿔야 하고, 모의투자 특유의 "호가를 관통해도 체결이 느릴 수 있다"는
특성이 실전에서는 다를 수 있으니 반드시 재검증할 것.

필요 환경변수: KIS_PAPER_APP_KEY, KIS_PAPER_APP_SECRET, KIS_PAPER_STOCK(계좌번호, 예: 50198831)

사용법:
    python live_order_executor.py <종목코드> <buy|sell> <수량> [--max-reprices N] [--max-seconds N]

예:
    python live_order_executor.py 472150 buy 4580
    python live_order_executor.py 472150 sell 4580
"""

import argparse
import asyncio
import json
import os
import time
from datetime import date

import requests
import websockets

from kis_price_client import BASE_URL, _headers, _get_with_retry  # 토큰 캐싱 등 인증 인프라 재사용

WS_URL = "ws://ops.koreainvestment.com:31000"  # 모의투자
APPROVAL_URL = f"{BASE_URL}/oauth2/Approval"
PRICE_TR_ID = "H0STCNT0"  # 국내주식 실시간체결가

ACNT_PRDT_CD = "01"

PRICE_FIELDS = (
    "종목코드|체결시간|현재가|전일대비부호|전일대비|전일대비율|가중평균가|시가|고가|저가|"
    "매도호가1|매수호가1"
).split("|")  # 필요한 앞부분만 파싱(뒤쪽 필드는 이 스크립트에서 안 씀)


def _cano() -> str:
    return os.environ["KIS_PAPER_STOCK"]


_last_call_at = 0.0
MIN_CALL_INTERVAL_SECONDS = 1.0  # KIS 초당 거래건수 제한 회피 (kis_price_client.py와 동일한 완화책)


def _throttle() -> None:
    global _last_call_at
    elapsed = time.monotonic() - _last_call_at
    if elapsed < MIN_CALL_INTERVAL_SECONDS:
        time.sleep(MIN_CALL_INTERVAL_SECONDS - elapsed)
    _last_call_at = time.monotonic()


def _raise_verbose(response: requests.Response) -> None:
    if response.status_code != 200:
        print(f"  HTTP {response.status_code} 응답 본문: {response.text[:500]}")
    response.raise_for_status()


def _get_asking_price(pdno: str) -> dict:
    """호가 10단 + 현재가 조회 (REST, 매번 최신값 필요할 때 호출)."""
    _throttle()
    response = _get_with_retry(
        f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
        headers={**_headers("FHKST01010200"), "tr_cont": ""},
        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": pdno},
    )
    _raise_verbose(response)
    data = response.json()
    if data["rt_cd"] != "0":
        raise RuntimeError(f"호가 조회 실패: {data['msg1']}")
    return data["output1"]


def _compute_sweep_price(book: dict, side: str, qty: int) -> int:
    """남은 수량을 다 받아줄 만큼 충분히 깊은 가격을 호가창에서 계산한다."""
    levels = range(1, 11)
    cumulative = 0
    if side == "buy":
        for level in levels:
            cumulative += int(book[f"askp_rsqn{level}"])
            if cumulative >= qty:
                return int(book[f"askp{level}"])
        return int(book["askp10"])  # 10단 잔량으로도 부족하면 일단 최대치로
    else:
        for level in levels:
            cumulative += int(book[f"bidp_rsqn{level}"])
            if cumulative >= qty:
                return int(book[f"bidp{level}"])
        return int(book["bidp10"])


def _place_order(pdno: str, side: str, qty: int, price: int) -> dict:
    tr_id = "VTTC0012U" if side == "buy" else "VTTC0011U"
    body = {
        "CANO": _cano(), "ACNT_PRDT_CD": ACNT_PRDT_CD, "PDNO": pdno,
        "ORD_DVSN": "00", "ORD_QTY": str(qty), "ORD_UNPR": str(price),
        "EXCG_ID_DVSN_CD": "KRX", "SLL_TYPE": "01" if side == "sell" else "", "CNDT_PRIC": "",
    }
    headers = {**_headers(tr_id), "tr_cont": "", "Accept": "text/plain", "charset": "UTF-8"}
    _throttle()
    response = requests.post(
        f"{BASE_URL}/uapi/domestic-stock/v1/trading/order-cash",
        headers=headers, data=json.dumps(body), timeout=20,
    )
    _raise_verbose(response)
    data = response.json()
    if data["rt_cd"] != "0":
        raise RuntimeError(f"주문 실패: {data['msg1']}")
    return data["output"]


def _cancel_order(krx_fwdg_ord_orgno: str, orgn_odno: str) -> dict:
    body = {
        "CANO": _cano(), "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "KRX_FWDG_ORD_ORGNO": krx_fwdg_ord_orgno, "ORGN_ODNO": orgn_odno,
        "ORD_DVSN": "00", "RVSE_CNCL_DVSN_CD": "02",
        "ORD_QTY": "0", "ORD_UNPR": "0", "QTY_ALL_ORD_YN": "Y", "EXCG_ID_DVSN_CD": "KRX",
    }
    headers = {**_headers("VTTC0013U"), "tr_cont": "", "Accept": "text/plain", "charset": "UTF-8"}
    _throttle()
    response = requests.post(
        f"{BASE_URL}/uapi/domestic-stock/v1/trading/order-rvsecncl",
        headers=headers, data=json.dumps(body), timeout=20,
    )
    _raise_verbose(response)
    data = response.json()
    if data["rt_cd"] != "0":
        raise RuntimeError(f"취소 실패: {data['msg1']}")
    return data["output"]


def _amend_order(krx_fwdg_ord_orgno: str, orgn_odno: str, new_price: int) -> dict:
    """취소 후 재주문(2회 호출) 대신 정정주문(1회 호출)으로 가격만 바꾼다 — 더 빠름.

    잔량 전부를 새 가격으로 정정한다(QTY_ALL_ORD_YN=Y라 ORD_QTY는 0으로 둬도 됨, 취소와 동일한 관례).
    """
    body = {
        "CANO": _cano(), "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "KRX_FWDG_ORD_ORGNO": krx_fwdg_ord_orgno, "ORGN_ODNO": orgn_odno,
        "ORD_DVSN": "00", "RVSE_CNCL_DVSN_CD": "01",
        "ORD_QTY": "0", "ORD_UNPR": str(new_price), "QTY_ALL_ORD_YN": "Y", "EXCG_ID_DVSN_CD": "KRX",
    }
    headers = {**_headers("VTTC0013U"), "tr_cont": "", "Accept": "text/plain", "charset": "UTF-8"}
    _throttle()
    response = requests.post(
        f"{BASE_URL}/uapi/domestic-stock/v1/trading/order-rvsecncl",
        headers=headers, data=json.dumps(body), timeout=20,
    )
    _raise_verbose(response)
    data = response.json()
    if data["rt_cd"] != "0":
        raise RuntimeError(f"정정 실패: {data['msg1']}")
    return data["output"]


def _check_fill(pdno: str, odno: str) -> dict:
    today = date.today().strftime("%Y%m%d")
    params = {
        "CANO": _cano(), "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "INQR_STRT_DT": today, "INQR_END_DT": today,
        "SLL_BUY_DVSN_CD": "00", "PDNO": pdno, "CCLD_DVSN": "00",
        "INQR_DVSN": "00", "INQR_DVSN_3": "00", "ORD_GNO_BRNO": "", "ODNO": odno,
        "INQR_DVSN_1": "", "CTX_AREA_FK100": "", "CTX_AREA_NK100": "", "EXCG_ID_DVSN_CD": "KRX",
    }
    _throttle()
    response = _get_with_retry(
        f"{BASE_URL}/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
        headers={**_headers("VTTC0081R"), "tr_cont": ""}, params=params,
    )
    _raise_verbose(response)
    data = response.json()
    if data["rt_cd"] != "0":
        raise RuntimeError(f"체결조회 실패: {data['msg1']}")
    match = next((r for r in data["output1"] if r["odno"] == odno), None)
    if match is None:
        return {"filled_qty": 0, "remaining_qty": None}
    return {"filled_qty": int(match["tot_ccld_qty"]), "remaining_qty": int(match["rmn_qty"])}


def _inquire_balance_raw() -> dict:
    params = {
        "CANO": _cano(), "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01",
        "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00",
        "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
    }
    _throttle()
    response = _get_with_retry(
        f"{BASE_URL}/uapi/domestic-stock/v1/trading/inquire-balance",
        headers={**_headers("VTTC8434R"), "tr_cont": ""}, params=params,
    )
    _raise_verbose(response)
    data = response.json()
    if data["rt_cd"] != "0":
        raise RuntimeError(f"잔고 조회 실패: {data['msg1']}")
    return data


def get_cash_balance() -> int:
    """주문가능현금(dnca_tot_amt)을 조회한다. F&G 신호의 '실탄 N%' 계산에 씀."""
    return int(_inquire_balance_raw()["output2"][0]["dnca_tot_amt"])


def get_holding(pdno: str) -> dict | None:
    """보유 종목의 수량/평균단가 조회(없으면 None). 체결 후 매매기록 로깅에 씀."""
    match = next((r for r in _inquire_balance_raw()["output1"] if r["pdno"] == pdno), None)
    if match is None:
        return None
    return {"qty": int(match["hldg_qty"]), "avg_price": float(match["pchs_avg_pric"])}


async def _get_approval_key() -> str:
    response = requests.post(
        APPROVAL_URL,
        headers={"content-type": "application/json"},
        json={
            "grant_type": "client_credentials",
            "appkey": os.environ["KIS_PAPER_APP_KEY"],
            "secretkey": os.environ["KIS_PAPER_APP_SECRET"],
        },
        timeout=20,
    )
    _raise_verbose(response)
    return response.json()["approval_key"]


class ChaseOrder:
    """실시간 틱을 보면서 체결될 때까지 지정가를 쫓아가는 상태 머신."""

    def __init__(self, pdno: str, side: str, total_qty: int, max_reprices: int, max_seconds: float):
        self.pdno = pdno
        self.side = side
        self.total_qty = total_qty
        self.max_reprices = max_reprices
        self.max_seconds = max_seconds

        self.filled_qty = 0
        self.reprice_count = 0
        self.order = None  # {"odno":, "krx_fwdg_ord_orgno":, "price":, "qty":}
        self.done = False
        self.last_fill_check = 0.0
        self.started_at = time.monotonic()

    def _remaining(self) -> int:
        return self.total_qty - self.filled_qty

    def _place_or_reprice(self) -> None:
        if self.order is not None:
            # 정정을 시도하기 직전에 다시 체결 여부를 확인한다 — 그 사이 전량 체결돼버리면
            # 정정할 잔량이 없어서 order_rvsecncl이 "정정 가능 수량 없음"으로 실패하는
            # 레이스 컨디션이 실전에서 확인됨(2026-07-23, 취소일 때도 동일하게 발생했었음).
            self._refresh_fill()
            if self.done:
                return

        book = _get_asking_price(self.pdno)
        price = _compute_sweep_price(book, self.side, self._remaining())

        if self.order is None:
            result = _place_order(self.pdno, self.side, self._remaining(), price)
            self.order = {
                "odno": result["ODNO"],
                "krx_fwdg_ord_orgno": result["KRX_FWDG_ORD_ORGNO"],
                "price": price,
            }
            print(f"  주문 접수: {self.side} {self._remaining()}주 @ {price}원 (주문번호 {result['ODNO']})")
            return

        if self.order["price"] == price:
            return  # 가격이 그대로면 정정 불필요

        # 취소 후 재주문(API 2회) 대신 정정주문(API 1회)으로 가격만 바꾼다 — 더 빠름
        # (사용자 피드백 2026-07-23: 취소+재주문은 그만큼 미체결로 노출되는 시간이 늘어남).
        print(f"  가격 이동 감지 — 정정주문 ({self.order['price']}원 -> {price}원)")
        try:
            result = _amend_order(self.order["krx_fwdg_ord_orgno"], self.order["odno"], price)
        except RuntimeError as exc:
            # 정정 직전 전량 체결됐을 가능성 — 다시 확인해서 진짜 끝났으면 넘어간다.
            self._refresh_fill()
            if self.done:
                return
            raise RuntimeError(f"정정 실패했는데 아직 미체결 잔량이 남아있음: {exc}") from exc
        self.reprice_count += 1
        self.order = {
            "odno": result["ODNO"],
            "krx_fwdg_ord_orgno": result["KRX_FWDG_ORD_ORGNO"],
            "price": price,
        }
        print(f"  정정 완료: {self.side} @ {price}원 (주문번호 {result['ODNO']})")

    def _refresh_fill(self) -> None:
        if self.order is None:
            return
        status = _check_fill(self.pdno, self.order["odno"])
        newly_filled = status["filled_qty"]
        if newly_filled > 0:
            self.filled_qty = self.total_qty - status["remaining_qty"] if status["remaining_qty"] is not None else self.total_qty
        print(f"  체결 확인: {self.filled_qty}/{self.total_qty}주")
        if self._remaining() <= 0:
            self.done = True

    def _give_up(self) -> None:
        """최대 시간/재주문 횟수를 넘겨서 중단할 때, 미체결 주문을 방치하지 않고 취소한다."""
        if self.order is None:
            return
        try:
            _cancel_order(self.order["krx_fwdg_ord_orgno"], self.order["odno"])
            print(f"  남은 미체결 주문(주문번호 {self.order['odno']})을 취소했습니다.")
        except RuntimeError as exc:
            self._refresh_fill()
            if not self.done:
                print(f"  경고: 중단 시 취소 실패 — 미체결 주문이 그대로 남아있을 수 있음: {exc}")

    async def run(self) -> None:
        approval_key = await _get_approval_key()
        subscribe_msg = json.dumps({
            "header": {"approval_key": approval_key, "custtype": "P", "tr_type": "1", "content-type": "utf-8"},
            "body": {"input": {"tr_id": PRICE_TR_ID, "tr_key": self.pdno}},
        })

        self._place_or_reprice()
        self._refresh_fill()
        if self.done:
            return

        async with websockets.connect(WS_URL, ping_interval=None) as ws:
            await ws.send(subscribe_msg)

            while not self.done:
                if time.monotonic() - self.started_at > self.max_seconds:
                    print(f"  최대 실행 시간({self.max_seconds}초) 초과 — 중단")
                    self._give_up()
                    return
                if self.reprice_count >= self.max_reprices:
                    print(f"  최대 재주문 횟수({self.max_reprices}) 초과 — 중단 (미체결 {self._remaining()}주 남음)")
                    self._give_up()
                    return

                data = await ws.recv()

                if data[0] == "0":
                    parts = data.split("|")
                    if parts[1] != PRICE_TR_ID:
                        continue
                    values = parts[3].split("^")
                    tick = dict(zip(PRICE_FIELDS, values))
                    tick_price = int(tick["매도호가1"] if self.side == "buy" else tick["매수호가1"])

                    stale = (
                        self.order is not None
                        and ((self.side == "buy" and tick_price > self.order["price"])
                             or (self.side == "sell" and tick_price < self.order["price"]))
                    )
                    if stale:
                        self._place_or_reprice()

                    now = time.monotonic()
                    if now - self.last_fill_check > 2.0:  # REST 호출 과다 방지
                        self.last_fill_check = now
                        self._refresh_fill()
                    continue

                payload = json.loads(data)
                tr_id = payload["header"]["tr_id"]
                if tr_id == "PINGPONG":
                    await ws.pong(data)
                    continue
                if payload["body"]["rt_cd"] == "1":
                    print(f"  구독 오류: {payload['body']['msg1']}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="실시간 호가를 쫓아가며 국내주식 주문을 체결시킨다 (모의투자).")
    parser.add_argument("stock_code")
    parser.add_argument("side", choices=["buy", "sell"])
    parser.add_argument("qty", type=int)
    parser.add_argument("--max-reprices", type=int, default=15)
    parser.add_argument("--max-seconds", type=float, default=300.0)
    args = parser.parse_args()

    print(f"{args.side} {args.qty}주 {args.stock_code} — 실시간 추격 주문 시작 (최대 {args.max_seconds}초, 재주문 {args.max_reprices}회)")
    chaser = ChaseOrder(args.stock_code, args.side, args.qty, args.max_reprices, args.max_seconds)
    await chaser.run()

    if chaser.done:
        print(f"전량 체결 완료: {chaser.filled_qty}/{chaser.total_qty}주 (재주문 {chaser.reprice_count}회)")
    else:
        print(f"미완료 종료: {chaser.filled_qty}/{chaser.total_qty}주 체결, {chaser._remaining()}주 미체결")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n중지했습니다.")
