"""balance_history(fabot-trade-journal, Supabase)가 조용히 며칠씩 안 쌓이는 걸 감시한다.

2026-08-31에 실제로 겪은 사고(무료 Render 서버가 자정 크론을 못 받아 11일 연속
스냅샷이 하나도 안 쌓였는데 아무도 몰랐던 일)의 재발 감지용이다. 매일 아침
한 번 실행해서, KIS/키움 각각 "가장 최근 스냅샷이 며칠 전인가"를 확인하고
너무 오래됐으면 텔레그램으로 경고한다.

필요 환경변수: SUPABASE_URL, SUPABASE_SERVICE_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
(전부 auto-trade.yml이 이미 쓰고 있는 것과 동일한 시크릿을 재사용한다)
"""

import os
from datetime import date, datetime, timedelta, timezone

import requests

STALE_AFTER_DAYS = 2  # 이보다 오래 안 쌓였으면 경고 (주말·공휴일 하루쯤은 정상 범위로 봐줌)
BROKERS = ["KIS", "Kiwoom"]


def _latest_snapshot_date(supabase_url: str, headers: dict, broker: str) -> date | None:
    resp = requests.get(
        f"{supabase_url}/rest/v1/balance_history",
        params={
            "select": "snapshot_date",
            "broker": f"eq.{broker}",
            "order": "snapshot_date.desc",
            "limit": "1",
        },
        headers=headers,
        timeout=20,
    )
    resp.raise_for_status()
    rows = resp.json()
    if not rows:
        return None
    return datetime.strptime(rows[0]["snapshot_date"], "%Y-%m-%d").date()


def _notify_telegram(text: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text[:4000]},
        timeout=20,
    )
    resp.raise_for_status()


def main() -> None:
    supabase_url = os.environ["SUPABASE_URL"]
    headers = {
        "apikey": os.environ["SUPABASE_SERVICE_KEY"],
        "Authorization": f"Bearer {os.environ['SUPABASE_SERVICE_KEY']}",
    }
    today = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9))).date()

    problems = []
    for broker in BROKERS:
        try:
            latest = _latest_snapshot_date(supabase_url, headers, broker)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"- {broker}: 조회 자체가 실패함 ({exc})")
            continue

        if latest is None:
            problems.append(f"- {broker}: balance_history에 기록이 하나도 없음")
            continue

        gap_days = (today - latest).days
        print(f"[{broker}] 최근 스냅샷: {latest} ({gap_days}일 전)")
        if gap_days > STALE_AFTER_DAYS:
            problems.append(f"- {broker}: 최근 스냅샷이 {latest}로 {gap_days}일째 안 쌓이고 있음")

    if problems:
        message = (
            "⚠️ FABOT 잔고 히스토리 감시 경고\n\n"
            + "\n".join(problems)
            + "\n\ncron-job.org(fabot, fabot-keepalive)가 잘 돌고 있는지, "
            "Render 서비스가 살아있는지 확인해주세요."
        )
        print(message)
        _notify_telegram(message)
    else:
        print("정상 — 모든 브로커가 최근 기록을 갖고 있음.")


if __name__ == "__main__":
    main()
