"""오늘의 F&G 신호를 판정하고, 실행 가능한 액션이면 실시간 추격 주문으로 바로 체결까지 진행한다.
국내(커버드콜)는 웹소켓 기반 live_order_executor.ChaseOrder, 해외(TQQQ)는 REST 폴링 기반
overseas_order_executor.OverseasChaseOrder를 쓴다. 체결되면 fabot-trade-journal(Supabase)에
매매 기록을 남겨서 다음 쿨다운 판정에 반영되게 한다.

**TQQQ(해외주식) 자동실행 관련 주의(2026-08-13 연결)**: 모의투자에서는 해외주식 실시간
웹소켓 시세가 지원되지 않아(2026-07-22 확인) 국내와 같은 방식은 못 쓰고, REST 호가를
주기적으로 폴링하는 overseas_order_executor.py를 쓴다. 이 실행기의 매수 경로는 실제
장중(22:30~05:00 KST)에 한 번 검증됐지만, 가격이 움직여 재주문(정정)하거나 시간초과로
포기(취소)하는 경로는 아직 실제 장중에 검증된 적이 없다 — 장이 닫혀있을 때는 호가가 전부
0이라 그 경로들을 미리 재현해볼 수도 없다. 처음 실행할 때는 결과를 사람이 지켜볼 것.

**커버드콜 종목코드 매핑 — 확정된 결정(2026-07-23)**: today_signal.py가 F&G 규칙상 지정한 티커
"TIGER 미국나스닥100타겟데일리커버드콜"의 실제 종목코드는 486290(KOSPI)이다. 하지만 이 상품은
분배금이 전부 배당소득세로 잡혀 실익이 떨어져서, 사용자가 세금상 유리한 472150(TIGER
배당커버드콜액티브)으로 실제 매매 대상을 바꾸기로 확정했다 — 종목코드를 못 찾아서 임시로 쓴 게
아니라 의도적인 상품 교체임. 쿨다운/매매기록은 today_signal.py의 COVERED_CALL_TICKER 이름을
그대로 키로 써서 기존 로직과의 일관성을 유지한다 — 실행 종목만 다르고 신호 판정 로직은 안 건드림.

필요 환경변수: KIS_PAPER_APP_KEY, KIS_PAPER_APP_SECRET, KIS_PAPER_STOCK
필요 파일: ../fabot-trade-journal/.env (SUPABASE_URL, SUPABASE_SERVICE_KEY) — cooldown.py가 읽음

사용법:
    python auto_trade_loop.py           # 신호 판정 + (해당되면) 실제 주문 실행
    python auto_trade_loop.py --dry-run # 신호만 판정하고 주문은 절대 넣지 않음
"""

import argparse
import asyncio
import sys

sys.stdout.reconfigure(encoding="utf-8")

import today_signal
import voice_briefing
from cooldown import log_trade
from live_order_executor import ChaseOrder, get_cash_balance, get_holding, _get_asking_price
from overseas_order_executor import (
    OverseasChaseOrder,
    get_overseas_cash_balance,
    get_overseas_holding,
    _get_asking_price as _get_overseas_asking_price,
)

COVERED_CALL_STOCK_CODE = "472150"  # 실행 종목 (today_signal.COVERED_CALL_TICKER와 다른 상품 — 위 docstring 참고)
COVERED_CALL_ALLOCATION = 0.20  # CLAUDE.md: 평시(F&G 35~65) 커버드콜 추가매수 = 실탄의 20%

# 매매기록/쿨다운 조회용 실제 종목명. today_signal.COVERED_CALL_TICKER("TIGER
# 미국나스닥100타겟데일리커버드콜")는 신호 판정상의 명목 종목명일 뿐, 실제로 매매하는
# 건 이 상품(472150)이다 — 매매기록에 신호상 이름을 그대로 쓰면 "이 종목을 샀다는데
# 계좌엔 없다"는 혼란이 생긴다(2026-08-14 사용자 확인). 그래서 기록/쿨다운 조회는
# 항상 이 실제 이름으로 통일한다.
COVERED_CALL_TRADE_KEY = "TIGER 배당커버드콜액티브(472150)"

KIS_ACCOUNT_LABEL = "KIS 모의투자"  # 매매기록의 account 필드 — 키움 모의계좌와 구분하기 위함

TQQQ_EXCG = "NASD"

# CLAUDE.md 확정 규칙: 매수(TQQQ) F&G<=25 -> 25% / <=20 -> 50% / <=15 -> 100%
_TQQQ_BUY_ALLOCATION_BY_SCORE = [(15, 1.0), (20, 0.5), (25, 0.25)]
# 매도(TQQQ) F&G>=75 -> 보유분 50% / >=80 -> 전량
_TQQQ_SELL_FRACTION_BY_SCORE = [(80, 1.0), (75, 0.5)]


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


def _compute_covered_call_qty(cash: int) -> int:
    book = _get_asking_price(COVERED_CALL_STOCK_CODE)
    reference_price = int(book["askp1"])
    budget = cash * COVERED_CALL_ALLOCATION
    return int(budget // reference_price)


def _not_executed(note: str) -> dict:
    return {"executed": False, "note": note}


async def _execute_covered_call_buy(today_info: dict, dry_run: bool) -> dict:
    cash = get_cash_balance()
    qty = _compute_covered_call_qty(cash)
    print(f"실탄(예수금) {cash:,}원 -> 20% 배분, 주문수량 {qty}주 ({COVERED_CALL_STOCK_CODE})")

    if qty <= 0:
        print("계산된 수량이 0주라 주문을 생략합니다.")
        return _not_executed("주문가능 수량이 0주라 실행하지 않았습니다")
    if dry_run:
        print("--dry-run 모드 — 실제 주문은 넣지 않음.")
        return _not_executed("dry-run 모드라 실제 주문은 넣지 않았습니다")

    # 스케줄 실행 창(15:19~15:30 KST, 장 마감 직전) 동안은 계속 쫓아가도 되게 여유 있게 잡음
    chaser = ChaseOrder(COVERED_CALL_STOCK_CODE, "buy", qty, max_reprices=60, max_seconds=630.0)
    await chaser.run()

    if not chaser.done:
        print(f"미완료 종료: {chaser.filled_qty}/{qty}주만 체결됨 — 매매기록은 실제 체결분만 남김.")
        if chaser.filled_qty <= 0:
            return _not_executed("주문이 체결되지 않았습니다")

    holding = get_holding(COVERED_CALL_STOCK_CODE)
    avg_price = holding["avg_price"] if holding else None
    if avg_price is None:
        print("경고: 체결 후 보유내역 조회에서 해당 종목을 못 찾음 — 매매기록을 남기지 못했습니다.")
        return _not_executed("체결 후 보유내역 조회에 실패했습니다")

    log_trade(
        ticker=COVERED_CALL_TRADE_KEY,
        action="buy",
        quantity=chaser.filled_qty,
        price=avg_price,
        fg_score=today_info["score"],
        memo=f"자동실행(auto_trade_loop.py), 신호상 명목종목='{today_signal.COVERED_CALL_TICKER}'",
        account=KIS_ACCOUNT_LABEL,
    )
    print(f"매매기록 저장 완료: buy {chaser.filled_qty}주 @ {avg_price}원 (ticker='{COVERED_CALL_TRADE_KEY}')")
    return {"executed": True, "action": "buy", "ticker": COVERED_CALL_TRADE_KEY,
             "qty": chaser.filled_qty, "price": avg_price}


async def _execute_tqqq_buy(today_info: dict, dry_run: bool) -> dict:
    book = _get_overseas_asking_price(today_signal.TQQQ_TICKER, TQQQ_EXCG)
    ref_price = float(book["pask1"])
    if ref_price <= 0:
        print("호가가 전부 0입니다 — 미국 정규장 시간(22:30~05:00 KST)이 아니라서 실행할 수 없습니다.")
        return _not_executed("미국 정규장 시간이 아니라 호가를 받을 수 없어 실행하지 않았습니다")

    cash = get_overseas_cash_balance(today_signal.TQQQ_TICKER, TQQQ_EXCG, ref_price)
    allocation = _tqqq_buy_allocation(today_info["score"])
    qty = int((cash * allocation) // ref_price)
    print(f"주문가능 외화현금 ${cash:,.2f} -> {allocation:.0%} 배분, 주문수량 {qty}주 ({today_signal.TQQQ_TICKER})")

    if qty <= 0:
        print("계산된 수량이 0주라 주문을 생략합니다.")
        return _not_executed("주문가능 수량이 0주라 실행하지 않았습니다")
    if dry_run:
        print("--dry-run 모드 — 실제 주문은 넣지 않음.")
        return _not_executed("dry-run 모드라 실제 주문은 넣지 않았습니다")

    chaser = OverseasChaseOrder(today_signal.TQQQ_TICKER, TQQQ_EXCG, "buy", qty,
                                 max_reprices=15, max_seconds=300.0, poll_interval=3.0)
    chaser.run()

    if not chaser.done:
        print(f"미완료 종료: {chaser.filled_qty}/{qty}주만 체결됨 — 매매기록은 실제 체결분만 남김.")
        if chaser.filled_qty <= 0:
            return _not_executed("주문이 체결되지 않았습니다")

    holding = get_overseas_holding(today_signal.TQQQ_TICKER, TQQQ_EXCG)
    avg_price = holding["avg_price"] if holding else None
    if avg_price is None:
        print("경고: 체결 후 보유내역 조회에서 해당 종목을 못 찾음 — 매매기록을 남기지 못했습니다.")
        return _not_executed("체결 후 보유내역 조회에 실패했습니다")

    log_trade(
        ticker=today_signal.TQQQ_TICKER,
        action="buy",
        quantity=chaser.filled_qty,
        price=avg_price,
        fg_score=today_info["score"],
        memo="자동실행(auto_trade_loop.py), 해외주식(TQQQ) REST 폴링 추격주문",
        account=KIS_ACCOUNT_LABEL,
    )
    print(f"매매기록 저장 완료: buy {chaser.filled_qty}주 @ ${avg_price} (ticker='{today_signal.TQQQ_TICKER}')")
    return {"executed": True, "action": "buy", "ticker": today_signal.TQQQ_TICKER,
             "qty": chaser.filled_qty, "price": avg_price}


async def _execute_tqqq_sell(today_info: dict, dry_run: bool) -> dict:
    holding = get_overseas_holding(today_signal.TQQQ_TICKER, TQQQ_EXCG)
    if holding is None or holding["qty"] <= 0:
        print(f"보유 중인 {today_signal.TQQQ_TICKER}가 없어 매도할 수 없습니다.")
        return _not_executed(f"보유 중인 {today_signal.TQQQ_TICKER}가 없어 실행하지 않았습니다")

    fraction = _tqqq_sell_fraction(today_info["score"])
    qty = int(holding["qty"] * fraction)
    print(f"보유 {holding['qty']}주 -> {fraction:.0%} 매도, 주문수량 {qty}주 ({today_signal.TQQQ_TICKER})")

    if qty <= 0:
        print("계산된 수량이 0주라 주문을 생략합니다.")
        return _not_executed("주문가능 수량이 0주라 실행하지 않았습니다")
    if dry_run:
        print("--dry-run 모드 — 실제 주문은 넣지 않음.")
        return _not_executed("dry-run 모드라 실제 주문은 넣지 않았습니다")

    chaser = OverseasChaseOrder(today_signal.TQQQ_TICKER, TQQQ_EXCG, "sell", qty,
                                 max_reprices=15, max_seconds=300.0, poll_interval=3.0)
    chaser.run()

    if not chaser.done:
        print(f"미완료 종료: {chaser.filled_qty}/{qty}주만 체결됨 — 매매기록은 실제 체결분만 남김.")
        if chaser.filled_qty <= 0:
            return _not_executed("주문이 체결되지 않았습니다")

    log_trade(
        ticker=today_signal.TQQQ_TICKER,
        action="sell",
        quantity=chaser.filled_qty,
        price=holding["avg_price"],
        fg_score=today_info["score"],
        memo="자동실행(auto_trade_loop.py), 해외주식(TQQQ) REST 폴링 추격주문 (가격은 매도 전 평균단가)",
        account=KIS_ACCOUNT_LABEL,
    )
    print(f"매매기록 저장 완료: sell {chaser.filled_qty}주 (ticker='{today_signal.TQQQ_TICKER}')")
    return {"executed": True, "action": "sell", "ticker": today_signal.TQQQ_TICKER,
             "qty": chaser.filled_qty, "price": holding["avg_price"]}


async def main() -> None:
    parser = argparse.ArgumentParser(description="오늘의 F&G 신호를 판정하고 실행 가능하면 바로 체결까지 진행한다.")
    parser.add_argument("--dry-run", action="store_true", help="신호만 판정하고 실제 주문은 넣지 않음")
    parser.add_argument(
        "--realtime", action="store_true",
        help="전일 확정 종가 대신 지금 이 순간의 실시간 계산값을 쓴다 (마감 직전 스케줄 실행용)",
    )
    args = parser.parse_args()

    today_info = today_signal.get_realtime_score() if args.realtime else today_signal.get_today_score()
    raw = today_signal.judge_raw_signal(today_info["score"])
    cooldown_key = COVERED_CALL_TRADE_KEY if raw.action == "buy_covered_call" else None
    result = today_signal.apply_cooldown(raw, cooldown_key=cooldown_key, account=KIS_ACCOUNT_LABEL)
    today_signal.log_result(today_info, raw, result)

    print("=== 오늘의 F&G 신호 ===")
    print(f"날짜: {today_info['date']}" + (" (실시간 조회 실패 — 마지막 캐시값 사용)" if today_info["stale"] else ""))
    print(f"F&G 점수: {today_info['score']:.1f} ({today_info['rating']})")
    print(f"원 판정: {raw.label}")
    print(f"최종 신호: {result['final_label']}")

    outcome = _not_executed("실행 대상 액션이 아니었습니다")

    if raw.action == "wait":
        print("-> 대기. 실행 없음.")
    elif result["cooldown"] and result["cooldown"]["in_cooldown"]:
        print(f"-> 쿨다운 중이라 실행 안 함 ({result['cooldown']['reason']})")
    elif raw.action == "buy_tqqq":
        outcome = await _execute_tqqq_buy(today_info, args.dry_run)
    elif raw.action == "sell_tqqq":
        outcome = await _execute_tqqq_sell(today_info, args.dry_run)
    elif raw.action == "buy_covered_call":
        outcome = await _execute_covered_call_buy(today_info, args.dry_run)
    else:
        print(f"-> 알 수 없는 액션({raw.action}) — 실행 안 함.")

    text, audio_path = await voice_briefing.synthesize_briefing_async(today_info, raw, result, outcome)
    print(f"\n음성 브리핑: {text}")
    print(f"음성 파일 저장: {audio_path}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n중지했습니다.")
