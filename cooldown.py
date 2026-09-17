"""fabot-trade-journal(Supabase)의 매매 기록을 조회해 쿨다운 상태를 판정합니다.

CLAUDE.md 규칙: TQQQ 매수, 커버드콜 추가매수는 각각 매수 후 4거래일 쿨다운이 있다
(2026-08-31, 3거래일에서 변경 — 분기별 재최적화 1차 결과 적용, fg-index/optimize_fabot_params.py).
매매 기록은 fabot-trade-journal 프로젝트의 Supabase `trades` 테이블에 이미 쌓이고 있으므로,
그 기록에서 종목별 마지막 매수일을 가져와 오늘까지 몇 거래일이 지났는지로 판정한다.
"""

import os
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

COOLDOWN_TRADING_DAYS = 4
JOURNAL_ENV_PATH = Path(__file__).resolve().parent.parent / "fabot-trade-journal" / ".env"


def _load_journal_env() -> dict:
    """SUPABASE_URL/SUPABASE_SERVICE_KEY가 환경변수로 이미 있으면 그걸 먼저 쓰고,
    없으면 이 프로젝트 원래 배치(../fabot-trade-journal/.env)에서 읽는다 — PoC를
    독립 저장소로 옮겨도(그 상대경로 파일이 없어도) 환경변수만 설정하면 그대로 돌게 하려는 것."""
    if "SUPABASE_URL" in os.environ and "SUPABASE_SERVICE_KEY" in os.environ:
        return {"SUPABASE_URL": os.environ["SUPABASE_URL"], "SUPABASE_SERVICE_KEY": os.environ["SUPABASE_SERVICE_KEY"]}

    env = {}
    for line in JOURNAL_ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


_TIER_LABEL = {1: "25%", 2: "50%", 3: "100%"}


def _trading_days_between(start: date, end: date) -> int:
    """start(포함) 다음날부터 end(포함)까지의 평일 수. 공휴일은 반영하지 않는다."""
    days = 0
    cursor = start + timedelta(days=1)
    while cursor <= end:
        if cursor.weekday() < 5:
            days += 1
        cursor += timedelta(days=1)
    return days


def get_last_buy(ticker: str, account: str | None = None) -> dict | None:
    """마지막 매수의 날짜와 그때의 F&G 점수. account를 안 넘기면 그 종목의 모든 계좌 매수기록을 다 본다. 2026-08-14부터
    KIS/키움 두 계좌가 같은 종목(472150)을 각자 독립적으로 자동매매하게 되면서,
    account까지 같이 필터링해야 한 계좌 매수가 다른 계좌 쿨다운에 영향을 안 준다."""
    env = _load_journal_env()
    url = f"{env['SUPABASE_URL']}/rest/v1/trades"
    params = {
        "select": "trade_date,fg_score",
        "ticker": f"eq.{ticker}",
        "action": "eq.buy",
        "order": "trade_date.desc",
        "limit": "1",
    }
    if account is not None:
        params["account"] = f"eq.{account}"
    headers = {
        "apikey": env["SUPABASE_SERVICE_KEY"],
        "Authorization": f"Bearer {env['SUPABASE_SERVICE_KEY']}",
    }
    response = requests.get(url, params=params, headers=headers, timeout=10)
    response.raise_for_status()
    rows = response.json()
    if not rows:
        return None
    return {
        "date": datetime.strptime(rows[0]["trade_date"], "%Y-%m-%d").date(),
        "fg_score": rows[0].get("fg_score"),
    }


def get_last_buy_date(ticker: str, account: str | None = None) -> date | None:
    """예전 이름 그대로 쓰는 호출부(대시보드 등)를 위해 남겨 둔 얇은 래퍼."""
    last = get_last_buy(ticker, account=account)
    return last["date"] if last else None


def check_cooldown(ticker: str, today: date | None = None, account: str | None = None,
                   current_tier: int | None = None) -> dict:
    """쿨다운 상태를 판정. DB 조회 실패 시 안전하게 '쿨다운 중'으로 취급(보수적 fail-safe).

    current_tier: 지금 신호의 매수 단계(today_signal.buy_tier). 넘기면 **단계 경계를
    넘어간 그 한 번에 한해** 대기 기간을 해제한다 — F&G 29(25% 단계)에 샀는데 다음날
    24(50% 단계)로 떨어지면, 대기 기간이 남아 있어도 그 매수는 한다.

    "공포가 깊으면 계속 산다"는 뜻이 **아니다**(2026-09-18 사용자가 명확히 함):
      - 같은 단계 안(29에 사고 28) → 해제 안 됨, 대기 기간 그대로
      - 얕아진 경우(24에 사고 29)  → 해제 안 됨
      - 경계를 넘을 때(29에 사고 24, 또는 24에 사고 19) → 그 한 번만 해제
    새 단계에서 사고 나면 그 매수가 새 기준이 되어 다시 대기 기간이 걸린다.
    """
    today = today or date.today()
    try:
        last_buy = get_last_buy(ticker, account=account)
    except Exception as exc:
        return {
            "ok": False,
            "in_cooldown": True,
            "escalated": False,
            "reason": f"매매기록 조회 실패({exc}) — 안전하게 대기 기간 중으로 처리",
            "last_buy_date": None,
            "elapsed_trading_days": None,
        }

    if last_buy is None:
        return {
            "ok": True,
            "in_cooldown": False,
            "escalated": False,
            "reason": "이전 매수 기록 없음",
            "last_buy_date": None,
            "elapsed_trading_days": None,
        }

    last_date = last_buy["date"]
    elapsed = _trading_days_between(last_date, today)
    in_cooldown = elapsed < COOLDOWN_TRADING_DAYS
    reason = (
        f"마지막 매수 {last_date} 이후 {elapsed}거래일 경과 "
        f"({'대기 기간 중' if in_cooldown else '대기 기간 종료'}, 기준 {COOLDOWN_TRADING_DAYS}거래일)"
    )

    escalated = False
    if in_cooldown and current_tier is not None:
        import today_signal  # 순환 import를 피하려고 여기서만 부른다

        last_score = last_buy.get("fg_score")
        last_tier = today_signal.buy_tier(float(last_score)) if last_score is not None else None
        if last_tier is None:
            reason += " / 지난 매수의 F&G를 몰라 단계 비교 불가 — 대기 기간 유지"
        elif current_tier > last_tier:
            escalated = True
            in_cooldown = False
            reason = (
                f"매수 단계 상향({_TIER_LABEL[last_tier]} -> {_TIER_LABEL[current_tier]}, "
                f"지난 매수 F&G {float(last_score):.0f}) — 대기 기간 해제. " + reason
            )

    return {
        "ok": True,
        "in_cooldown": in_cooldown,
        "escalated": escalated,
        "reason": reason,
        "last_buy_date": last_date,
        "last_buy_fg_score": last_buy.get("fg_score"),
        "elapsed_trading_days": elapsed,
    }


def log_trade(
    ticker: str, action: str, quantity: float, price: float,
    fg_score: float | None = None, memo: str = "", account: str | None = None,
) -> None:
    """체결된 매매를 fabot-trade-journal의 Supabase trades 테이블에 기록한다.

    다음 번 check_cooldown() 호출이 이 기록을 바로 봐야 하므로, 자동매매 실행 스크립트는
    주문이 실제로 체결된 직후 반드시 이걸 호출해야 한다(안 하면 쿨다운이 영원히 안 걸림).

    account: 어느 증권사/모의·실전 계좌에서 한 매매인지("KIS 모의투자" 등). 2026-08-14
    추가 — KIS와 키움 모의계좌가 우연히 같은 종목을 동시에 들고 있어서, 매매기록도
    계좌별로 구분해야 화면에서 안 헷갈린다.
    """
    env = _load_journal_env()
    url = f"{env['SUPABASE_URL']}/rest/v1/trades"
    headers = {
        "apikey": env["SUPABASE_SERVICE_KEY"],
        "Authorization": f"Bearer {env['SUPABASE_SERVICE_KEY']}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    body = {
        "trade_date": date.today().isoformat(),
        "ticker": ticker,
        "action": action,
        "quantity": quantity,
        "price": price,
        "fg_score": fg_score,
        "memo": memo,
        "account": account,
    }
    response = requests.post(url, json=body, headers=headers, timeout=10)
    response.raise_for_status()


if __name__ == "__main__":
    for t in ["TQQQ", "커버드콜"]:
        result = check_cooldown(t)
        print(f"[{t}] {result['reason']}")
