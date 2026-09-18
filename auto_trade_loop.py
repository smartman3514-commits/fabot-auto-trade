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
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.stdout.reconfigure(encoding="utf-8")

import account_summary
import today_signal
import voice_briefing
from cooldown import log_trade
from live_order_executor import ChaseOrder, get_cash_balance, get_holding, _get_asking_price
from overseas_order_executor import (
    OverseasChaseOrder,
    get_overseas_cash_balance,
    get_overseas_order_capacity,
    get_overseas_holding,
    _get_asking_price as _get_overseas_asking_price,
)

COVERED_CALL_STOCK_CODE = "472150"  # 실행 종목 (today_signal.COVERED_CALL_TICKER와 다른 상품 — 위 docstring 참고)
# 배분 비율은 today_signal.COVERED_CALL_ALLOCATION 하나로 통일한다 — 예전엔 여기 따로
# 상수를 뒀는데, judge_raw_signal()의 라벨 텍스트("커버드콜 추가매수 20%")가 그 상수를
# 안 보고 하드코딩돼 있어서 값만 10%로 바꿨을 때 화면엔 여전히 "20%"라고 찍히는
# 불일치가 있었다(2026-08-22 발견, cooldown 3일/10% 변경 작업 중).
COVERED_CALL_ALLOCATION = today_signal.COVERED_CALL_ALLOCATION

# 매매기록/쿨다운 조회용 실제 종목명. today_signal.COVERED_CALL_TICKER("TIGER
# 미국나스닥100타겟데일리커버드콜")는 신호 판정상의 명목 종목명일 뿐, 실제로 매매하는
# 건 이 상품(472150)이다 — 매매기록에 신호상 이름을 그대로 쓰면 "이 종목을 샀다는데
# 계좌엔 없다"는 혼란이 생긴다(2026-08-14 사용자 확인). 그래서 기록/쿨다운 조회는
# 항상 이 실제 이름으로 통일한다.
COVERED_CALL_TRADE_KEY = "TIGER 배당커버드콜액티브(472150)"

KIS_ACCOUNT_LABEL = "KIS 모의투자"  # 매매기록의 account 필드 — 키움 모의계좌와 구분하기 위함

TQQQ_EXCG = "NASD"

# 임계값은 today_signal.py의 BUY_THRESHOLDS/SELL_THRESHOLDS를 그대로 따른다 — 여기 따로
# 하드코딩해뒀던 옛 값(15/20/25, 75/80)이 2026-08-31 임계값 변경(20/25/30, 72/77) 때 같이
# 안 바뀌어서, F&G 26~30점 구간에서 신호는 "매수 25%"로 정확히 잡히고도 배분 비율 계산에서
# ValueError로 조용히 실패해 실제 매수가 여러 번 누락되는 사고가 있었다(2026-09-17 발견).
# 두 파일에 같은 상수를 따로 들고 있으면 이 사고가 반드시 재발하므로, 이제 한 곳(today_signal)
# 만 고치면 여기도 같이 바뀌도록 import해서 쓴다.
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


def _compute_covered_call_qty(cash: int) -> int:
    book = _get_asking_price(COVERED_CALL_STOCK_CODE)
    reference_price = int(book["askp1"])
    budget = cash * COVERED_CALL_ALLOCATION
    return int(budget // reference_price)


KST = timezone(timedelta(hours=9))
# 커버드콜(472150, KOSPI)은 국내장 종가 기준으로 매매하기로 확정된 원칙(사용자 지정,
# 2026-08-31)이라, 실행 시각이 이 창을 벗어나면 사유를 몰라도 일단 건너뛴다. 2026-08-28에
# 예정에 없던 시각(12:53 KST)에 커버드콜 매수가 실행된 사례가 실제로 있었음 — 원인은
# 못 찾았지만(로컬 작업 스케줄러에도 없었음), 언제 실행되든 이 가드가 있으면 잘못된
# 시각의 매수 자체를 막을 수 있다. 실행 창은 15:19~15:30이지만, 스케줄 시작 지연을
# 고려해 앞뒤로 여유를 둔다.
DOMESTIC_CLOSE_WINDOW_KST = ((15, 15), (15, 35))


def _within_domestic_close_window(now: datetime | None = None) -> bool:
    now = (now or datetime.now(timezone.utc)).astimezone(KST)
    (start_h, start_m), (end_h, end_m) = DOMESTIC_CLOSE_WINDOW_KST
    start = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    return start <= now <= end


# TQQQ(나스닥) 마감은 항상 16:00 America/New_York다 — 서머타임(EDT/EST) 계산은
# zoneinfo에게 통째로 맡기고, 우리는 그 지역 시각 16:00만 고정하면 된다(2026-08-31,
# 이전엔 KST로 04:40~06:10을 하드코딩했는데 그게 서머타임 두 경우를 수동으로 나열한
# 것이었다 — zoneinfo를 쓰면 그 나열 자체가 필요 없어진다).
#
# 이 워크플로우의 크론("15,45 19,20,21 * * 0-5", 아래 auto-trade.yml)은 UTC 고정이라
# 자체적으로 서머타임을 못 따라가므로, 19:15~21:45 UTC(04:15~06:45 KST) 사이를
# 30분 간격으로 여러 번 실행되게 해뒀다 — 그중 실제 마감(EDT 05:00 KST 또는 EST
# 06:00 KST) 앞뒤 20분 안에 들어오는 실행만 여기서 통과시키고, 나머지는 건너뛴다.
NYSE_TZ = ZoneInfo("America/New_York")
TQQQ_WINDOW_BEFORE = timedelta(minutes=20)
TQQQ_WINDOW_AFTER = timedelta(minutes=20)


def _today_nyse_close_kst(now_utc: datetime) -> datetime:
    """오늘 날짜 기준 16:00 America/New_York을 계산해 KST로 변환한다.
    zoneinfo가 그 날짜의 실제 서머타임 여부를 알아서 반영한다."""
    local_today = now_utc.astimezone(NYSE_TZ)
    close_local = local_today.replace(hour=16, minute=0, second=0, microsecond=0)
    return close_local.astimezone(KST)


def _within_tqqq_close_window(now: datetime | None = None) -> bool:
    now_utc = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)
    close_kst = _today_nyse_close_kst(now_utc)
    now_kst = now_utc.astimezone(KST)
    return close_kst - TQQQ_WINDOW_BEFORE <= now_kst <= close_kst + TQQQ_WINDOW_AFTER


def _not_executed(note: str) -> dict:
    return {"executed": False, "note": note}


async def _execute_covered_call_buy(today_info: dict, dry_run: bool) -> dict:
    if not _within_domestic_close_window():
        now_kst = datetime.now(timezone.utc).astimezone(KST).strftime("%H:%M")
        (sh, sm), (eh, em) = DOMESTIC_CLOSE_WINDOW_KST
        print(f"지금은 {now_kst} KST — 국내장 종가 실행 창({sh:02d}:{sm:02d}~{eh:02d}:{em:02d})이 아니라 건너뜁니다.")
        return _not_executed(f"실행 창({sh:02d}:{sm:02d}~{eh:02d}:{em:02d} KST) 밖이라 실행하지 않았습니다 (현재 {now_kst})")

    cash = get_cash_balance()
    qty = _compute_covered_call_qty(cash)
    print(f"실탄(예수금) {cash:,}원 -> {COVERED_CALL_ALLOCATION:.0%} 배분, 주문수량 {qty}주 ({COVERED_CALL_STOCK_CODE})")

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


def _tqqq_window_message() -> str:
    now_utc = datetime.now(timezone.utc)
    now_kst = now_utc.astimezone(KST)
    close_kst = _today_nyse_close_kst(now_utc)
    return (
        f"실행 창(오늘 마감 {close_kst.strftime('%H:%M')} KST 전후 20분) 밖 "
        f"(현재 {now_kst.strftime('%H:%M')})"
    )


async def _execute_tqqq_buy(today_info: dict, dry_run: bool) -> dict:
    if not _within_tqqq_close_window():
        msg = _tqqq_window_message()
        print(f"지금은 {msg} — 건너뜁니다.")
        return _not_executed(f"{msg}이라 실행하지 않았습니다")

    book = _get_overseas_asking_price(today_signal.TQQQ_TICKER, TQQQ_EXCG)
    ref_price = float(book["pask1"])
    if ref_price <= 0:
        print("호가가 전부 0입니다 — 미국 정규장 시간(22:30~05:00 KST)이 아니라서 실행할 수 없습니다.")
        return _not_executed("미국 정규장 시간이 아니라 호가를 받을 수 없어 실행하지 않았습니다")

    capacity = get_overseas_order_capacity(today_signal.TQQQ_TICKER, TQQQ_EXCG, ref_price)
    cash, broker_max = capacity["cash"], capacity["max_qty"]
    allocation = _tqqq_buy_allocation(today_info["score"])
    qty = int((cash * allocation) // ref_price)
    print(f"주문가능 외화현금 ${cash:,.2f} -> {allocation:.0%} 배분, 주문수량 {qty}주 ({today_signal.TQQQ_TICKER})")
    # 증권사가 계산한 한도를 넘지 않게 자른다. 수수료 몫 때문에 우리 계산이 한도보다 조금
    # 크게 나온다(2026-09-18 실측: 우리 1,048주 vs KIS 한도 1,037주). 100% 단계에서만
    # 실제로 걸리지만, 그게 가장 중요한 매수라 항상 확인한다.
    if broker_max is not None and qty > broker_max:
        print(f"  증권사 한도({broker_max}주)를 넘어 {qty}주 -> {broker_max}주로 줄입니다(수수료 몫).")
        qty = broker_max

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
    if not _within_tqqq_close_window():
        msg = _tqqq_window_message()
        print(f"지금은 {msg} — 건너뜁니다.")
        return _not_executed(f"{msg}이라 실행하지 않았습니다")

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

    today_info = today_signal.get_cnn_score() if args.realtime else today_signal.get_today_score()
    raw = today_signal.judge_raw_signal(today_info["score"])
    cooldown_key = COVERED_CALL_TRADE_KEY if raw.action == "buy_covered_call" else None
    result = today_signal.apply_cooldown(raw, cooldown_key=cooldown_key, account=KIS_ACCOUNT_LABEL,
                                         score=today_info["score"])
    today_signal.log_result(today_info, raw, result)

    print("=== 오늘의 F&G 신호 ===")
    print(f"날짜: {today_info['date']}" + (" (실시간 조회 실패 — 마지막 캐시값 사용)" if today_info["stale"] else ""))
    try:
        print(account_summary.format_composition_line(account_summary.get_account_composition("KIS")))
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
