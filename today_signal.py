"""오늘의 F&G 지수를 확인하고, FABOT 매매 규칙에 따른 신호를 판정합니다.

매매 규칙 (CLAUDE.md 기준, 2026-08-31 분기 재최적화 적용):
- TQQQ 매수: F&G<=30 -> 실탄 25% / F&G<=25 -> 50% / F&G<=20 -> 100% (매수 후 4거래일 쿨다운)
- TQQQ 매도: F&G>=72 -> 보유분 50% / F&G>=77 -> 잔량 전량 (쿨다운 없음)
- 커버드콜 추가매수: F&G 35~65(평시) -> 실탄 10% (매수 후 4거래일 쿨다운)

임계값은 F&G<=30/>=70 구간에서만 AI 재량으로 조정 가능하도록 사용자가 정한 범위 안에서,
10년 블록부트스트랩 랜덤서치(300개 후보)로 찾은 값이다(코드: fg-index/optimize_fabot_params.py).
기존 규칙(25/20/15, 75/80, 3일) 대비 평균 CAGR·MDD는 거의 동일하고, 최악의 경우 MDD가
-68.1% -> -60.4%로 개선됨. 다음 재검토는 2026-11-30(3개월 뒤).

쿨다운 판정은 fabot-trade-journal(Supabase)의 실제 매매 기록을 조회해서 한다.
CNN F&G 실시간 조회가 실패하면 fg_index.csv의 마지막 값으로 대체한다(오래된 값임을 표시).
매 실행 결과는 signal_log.csv에 누적 기록한다.
"""

import csv
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import requests

from cooldown import check_cooldown
from price_proxy import WEIGHTS_VIXY, INTERCEPT_VIXY, compute_price_based_fg

# fg-dashboard가 쓰는 공개 프로젝트 — 대시보드 index.html/login.html에도 그대로 노출된
# 공개 anon 키라 여기서도 그대로 재사용한다(비밀값 아님).
_LIVE_SUPABASE_URL = "https://wckohdcjpjlzeaiehmzs.supabase.co"
_LIVE_SUPABASE_KEY = "sb_publishable_4fPOT_f1VwEvH4gEXumgjg_JH-7X_mY"

TQQQ_TICKER = "TQQQ"
COVERED_CALL_TICKER = "TIGER 미국나스닥100타겟데일리커버드콜"  # 종목코드 486290(KOSPI)

# 평시(F&G 35~65) 커버드콜 추가매수 배분 — 실제 주문 수량 계산(auto_trade_loop*.py의
# _compute_covered_call_qty)도 이 값을 그대로 쓴다. 예전엔 auto_trade_loop.py/
# auto_trade_loop_kiwoom.py 각자 자기 파일에 COVERED_CALL_ALLOCATION = 0.20을
# 따로 들고 있었는데, judge_raw_signal()의 라벨 문자열("커버드콜 추가매수 20%")은
# 하드코딩된 별개의 텍스트라서, 10%로 바꿨을 때 실제 주문액은 반으로 줄었는데
# 화면·로그엔 여전히 "20%"라고 찍히는 불일치가 있었다(2026-08-22 발견). 이제 이
# 상수 하나로 통일해서, 라벨도 f"{COVERED_CALL_ALLOCATION:.0%}"로 항상 실제
# 값과 같이 움직이게 한다.
COVERED_CALL_ALLOCATION = 0.10  # 2026-08-22, 20%에서 변경(10년 시뮬레이션으로 확인 후 결정)

# TQQQ 매수/매도 임계값 — 이름 붙은 상수로 빼서 judge_raw_signal()과 텔레그램 알림
# (.github/workflows/auto-trade.yml)이 같은 값을 그대로 읽게 한다.
# 2026-08-31 분기 재최적화 1차 결과 적용(기존 25/20/15, 75/80에서 변경).
BUY_THRESHOLDS = (20, 25, 30)   # 이하일 때 각각 100%/50%/25% 매수
SELL_THRESHOLDS = (72, 77)      # 이상일 때 각각 50%/전량 매도
COVERED_CALL_ZONE = (35, 65)    # 이 구간(평시)이면 커버드콜 추가매수
# 실제 매수 종목은 이 티커가 아니라 472150(TIGER 배당커버드콜액티브)로 확정됨(2026-07-23) —
# 486290은 분배금이 전부 배당소득세로 잡혀 세금상 불리해서 사용자가 의도적으로 바꾼 것.
# 신호 판정/쿨다운 키는 이 상수(486290 쪽 이름)를 그대로 쓰고, 실행 종목코드 매핑은
# auto_trade_loop.py의 COVERED_CALL_STOCK_CODE에서 관리한다.

FG_INDEX_CSV = Path(__file__).resolve().parent / "fg_index.csv"
PRICE_CACHE_CSV = Path(__file__).resolve().parent / "price_cache.csv"
LOG_CSV = Path(__file__).resolve().parent / "signal_log.csv"


@dataclass
class RawSignal:
    label: str
    action: str  # "buy_tqqq" | "sell_tqqq" | "buy_covered_call" | "wait"
    ticker: str | None


def buy_tier(score: float) -> int | None:
    """매수 단계. 3 = 100%(극단적 공포), 2 = 50%, 1 = 25%, None = 매수 구간 아님.

    대기 기간 해제는 **단계를 넘어가는 그 한 번**에만 적용된다(2026-09-18 사용자 규칙).
    "공포가 깊으면 계속 산다"가 아니다 — 같은 단계 안에서는 대기 기간이 그대로 지켜지고,
    30 -> 25처럼 경계를 넘을 때만 그 한 번의 매수를 위해 해제된다. 새 단계에서 사고 나면
    그 매수가 새 기준이 되어 다시 대기 기간이 걸린다.
    """
    t100, t50, t25 = BUY_THRESHOLDS
    if score <= t100:
        return 3
    if score <= t50:
        return 2
    if score <= t25:
        return 1
    return None


def judge_signal(score: float) -> str:
    """live_fg.py / export_dashboard_data.py가 쓰는 기존 API (라벨 문자열만 필요, 쿨다운 미반영)."""
    return judge_raw_signal(score).label


def judge_raw_signal(score: float) -> RawSignal:
    t100, t50, t25 = BUY_THRESHOLDS
    s50, s100 = SELL_THRESHOLDS
    if score <= t100:
        return RawSignal("TQQQ 매수 100% (극단적 공포)", "buy_tqqq", TQQQ_TICKER)
    if score <= t50:
        return RawSignal("TQQQ 매수 50%", "buy_tqqq", TQQQ_TICKER)
    if score <= t25:
        return RawSignal("TQQQ 매수 25%", "buy_tqqq", TQQQ_TICKER)
    if score >= s100:
        return RawSignal("TQQQ 매도 전량 (극단적 탐욕)", "sell_tqqq", None)
    if score >= s50:
        return RawSignal("TQQQ 매도 50%", "sell_tqqq", None)
    cc_lo, cc_hi = COVERED_CALL_ZONE
    if cc_lo <= score <= cc_hi:
        return RawSignal(f"커버드콜 추가매수 {COVERED_CALL_ALLOCATION:.0%} (평시)", "buy_covered_call", COVERED_CALL_TICKER)
    return RawSignal("대기 (매수/매도 조건 밖)", "wait", None)


def get_today_score() -> dict:
    """종가 기준으로 F&G를 계산한다 — 장중 실시간 시세(CNN 라이브값·키움 실시간 호가)는
    절대 쓰지 않는다. price_cache.csv는 refresh_price_cache.py가 장 마감 후 하루 1번
    받아오는 확정 종가라, 이 파일을 그대로 계산에 쓰면 자연히 "가장 최근 완결된 거래일의
    종가"만 반영된다 (2026-07-31, 사용자 요청으로 CNN 실시간값 대신 이 방식으로 전환).
    price_cache.csv 조회 실패 시에만 fg_index.csv의 마지막 값으로 대체(fail-safe).
    """
    try:
        df = pd.read_csv(PRICE_CACHE_CSV, index_col="date", parse_dates=True).sort_index()
        fg_series = compute_price_based_fg(
            qqq=df["QQQ"], vix=df["VIXY"], ief=df["IEF"], hyg=df["HYG"], lqd=df["LQD"],
            weights=WEIGHTS_VIXY, intercept=INTERCEPT_VIXY,
        )
        last_date = fg_series.index[-1]
        return {
            "date": last_date.date(),
            "score": float(fg_series.iloc[-1]),
            "rating": "price_based_close",
            "stale": False,
        }
    except Exception as exc:
        df = pd.read_csv(FG_INDEX_CSV, index_col=0)
        last_row = df.iloc[-1]
        last_date = datetime.strptime(str(df.index[-1]), "%Y-%m-%d").date()
        return {
            "date": last_date,
            "score": float(last_row["final_score"]),
            "rating": "unknown",
            "stale": True,
            "error": str(exc),
        }


_CNN_RATING_KR = {
    "extreme fear": "극단적 공포",
    "fear": "공포",
    "neutral": "중립",
    "greed": "탐욕",
    "extreme greed": "극단적 탐욕",
}


def _zone_from_score(score: float) -> str:
    """CNN이 rating 문자열을 안 주는 경우(자체 계산 fallback)를 위한 근사 구간
    — CNN 공식 5단계 경계와 대략 맞춘 값(0-24/25-44/45-55/56-75/76-100)."""
    if score <= 24:
        return "극단적 공포"
    if score <= 44:
        return "공포"
    if score <= 55:
        return "중립"
    if score <= 75:
        return "탐욕"
    return "극단적 탐욕"


def get_cnn_score() -> dict:
    """CNN 공식 Fear & Greed Index를 실시간으로 가져온다(인선님 요청, 2026-08-21) —
    자체 계산(price_based)은 6개월~1년치 통계가 쌓여야 신뢰할 만한 balance를 찾을 수
    있어서, 그때까지는 실제 CNN 지수를 신호 판정 기준으로 쓴다. CNN은 기본 User-Agent로
    요청하면 418("I'm a teapot")로 막아서, 브라우저와 비슷한 헤더를 보내야 한다.
    실패하면 get_realtime_score()(자체 계산)로 안전하게 대체한다."""
    try:
        response = requests.get(
            "https://production.dataviz.cnn.io/index/fearandgreed/graphdata/2020-09-18",
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://edition.cnn.com/markets/fear-and-greed",
            },
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()["fear_and_greed"]
        return {
            "date": datetime.fromisoformat(data["timestamp"]).date(),
            "score": float(data["score"]),
            "rating": "cnn_live",
            "zone": _CNN_RATING_KR.get(data["rating"], data["rating"]),
            "stale": False,
        }
    except Exception as exc:
        fallback = get_realtime_score()
        fallback["zone"] = _zone_from_score(fallback["score"])
        fallback["stale"] = True
        fallback["error"] = f"CNN 실시간 조회 실패, 자체 계산(price_based)으로 대체: {exc}"
        return fallback


def get_realtime_score() -> dict:
    """장 마감 직전 실행(auto_trade_loop.py의 마감 10분 전 스케줄)처럼, 오늘 확정될 종가를
    기다릴 수 없고 "지금 이 순간" 값이 필요한 경우에 쓴다. get_today_score()(전일 확정
    종가 기준, 2026-07-31 사용자 요청으로 확정된 기본 방식)와는 용도가 다르다 — 일반
    신호 판정/기록에는 get_today_score()를 그대로 쓰고, 이 함수는 실시간 실행 전용이다.
    dashboard/scripts/update_live_score.py가 5분마다 계산해 저장하는 price_based 최신값을
    그대로 읽어온다(같은 계산을 여기서 중복하지 않음). 실패 시 get_today_score()로 대체.
    """
    try:
        response = requests.get(
            f"{_LIVE_SUPABASE_URL}/rest/v1/live_scores",
            headers={"apikey": _LIVE_SUPABASE_KEY, "Authorization": f"Bearer {_LIVE_SUPABASE_KEY}"},
            params={"source": "eq.price_based", "order": "computed_at.desc", "limit": "1"},
            timeout=20,
        )
        response.raise_for_status()
        rows = response.json()
        if not rows:
            raise RuntimeError("실시간 계산값이 아직 하나도 없음")
        row = rows[0]
        return {
            "date": datetime.fromisoformat(row["computed_at"]).date(),
            "score": float(row["score"]),
            "rating": "realtime_price_based",
            "stale": False,
        }
    except Exception as exc:
        fallback = get_today_score()
        fallback["stale"] = True
        fallback["error"] = f"실시간 조회 실패, 전일 종가로 대체: {exc}"
        return fallback


def apply_cooldown(raw: RawSignal, cooldown_key: str | None = None, account: str | None = None,
                   score: float | None = None) -> dict:
    """cooldown_key: 실제 매매기록에 쓰인 키가 raw.ticker(신호상 명목 종목명)와 다를 때
    (예: 커버드콜은 신호상 486290 이름을 쓰지만 실제로는 472150을 매매함) 호출부가
    실제 매매기록 조회용 키를 넘긴다. 안 넘기면 raw.ticker를 그대로 쓴다.

    score: 지금 F&G 점수. 넘기면 TQQQ 매수 단계(25%/50%/100%)를 계산해서, 지난 매수보다
    더 깊은 단계면 대기 기간을 해제한다(2026-09-18). 안 넘기면 예전과 똑같이 동작한다.

    account: 이 계좌의 매매기록만 놓고 쿨다운을 판정한다. 2026-08-14부터 KIS와 키움이
    각자 독립적으로 같은 종목을 자동매매하게 되어서, account를 안 넘기면 한 계좌의
    매수가 다른 계좌의 쿨다운까지 걸어버리는 문제가 생긴다."""
    if raw.action not in ("buy_tqqq", "buy_covered_call"):
        return {"final_label": raw.label, "cooldown": None}

    # TQQQ만 단계(25%/50%/100%)가 있다 — 단계가 깊어지면 대기 기간을 해제한다.
    # 커버드콜은 단계가 없어서(평시 10% 하나뿐) current_tier를 넘기지 않는다.
    current_tier = buy_tier(score) if raw.action == "buy_tqqq" and score is not None else None
    cd = check_cooldown(cooldown_key or raw.ticker, account=account, current_tier=current_tier)
    if cd["in_cooldown"]:
        return {
            "final_label": f"대기 기간 중 — 조건은 '{raw.label}'이지만 {cd['reason']}",
            "cooldown": cd,
        }
    return {"final_label": raw.label, "cooldown": cd}


def log_result(today_info: dict, raw: RawSignal, result: dict) -> None:
    is_new = not LOG_CSV.exists()
    with open(LOG_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow([
                "run_at", "fg_date", "fg_score", "fg_rating", "stale",
                "raw_signal", "final_signal", "cooldown_reason",
            ])
        writer.writerow([
            datetime.now().isoformat(timespec="seconds"),
            today_info["date"],
            today_info["score"],
            today_info["rating"],
            today_info["stale"],
            raw.label,
            result["final_label"],
            result["cooldown"]["reason"] if result["cooldown"] else "",
        ])


def main() -> None:
    today_info = get_today_score()
    raw = judge_raw_signal(today_info["score"])
    result = apply_cooldown(raw)
    log_result(today_info, raw, result)

    print("=== 오늘의 F&G 신호 리포트 ===")
    print(f"날짜: {today_info['date']}" + (" (실시간 조회 실패 — fg_index.csv 마지막 값 사용)" if today_info["stale"] else ""))
    if today_info["stale"]:
        print(f"  실패 사유: {today_info['error']}")
    print(f"F&G 점수: {today_info['score']:.1f} ({today_info['rating']})")
    print(f"원 판정: {raw.label}")
    print(f"최종 신호: {result['final_label']}")
    if result["cooldown"] and not result["cooldown"]["in_cooldown"] and result["cooldown"]["last_buy_date"]:
        print(f"  ({result['cooldown']['reason']})")


if __name__ == "__main__":
    main()
