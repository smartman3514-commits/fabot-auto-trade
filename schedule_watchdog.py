"""스케줄 워치독 — 하루 4번 도는 브리핑, 자동매매 루프가 "조용히 안 도는" 걸 감시한다.

balance_watchdog.py(잔고 기록 공백 감시)와 같은 패턴이다. 각 스케줄은 실행될 때마다
(성공/실패/매매유무와 무관하게) schedule_heartbeats에 한 줄을 남기게 되어 있고
(auto-trade.yml의 "Record heartbeat" 스텝, briefing-agent server.py의
_run_scheduled_briefing), 이 스크립트는 그 마지막 기록이 예상 주기보다 오래됐으면
텔레그램으로 경고한다. 2026-09-10, "스케줄이 안 돌면 사람이 우연히 알아채는 게 유일한
안전망"이라는 문제를 해결하기 위해 추가함.

이 스크립트 자체는 별도 GitHub Actions 워크플로(schedule-watchdog.yml)에서 몇 시간마다
돈다 — 감시 대상(Render, cron-job.org)과 다른 인프라에서 돌아야 감시하는 쪽까지 같이
죽는 걸 피할 수 있다.
"""

import os
from datetime import datetime, timedelta, timezone

import requests

# job -> 최대 허용 공백(시간). 실제 주기보다 넉넉하게 잡아서(주말·서머타임 경계 등)
# 오탐을 줄이되, "하루 이상 통째로 안 돔"은 반드시 잡도록 함.
EXPECTED_JOBS = {
    "briefing_us_close": 30,
    "briefing_kr_open": 30,
    "briefing_kr_close": 30,
    "briefing_us_open": 30,
    "auto_trade_workflow": 36,
}


def _last_heartbeat(supabase_url: str, service_key: str, job: str) -> datetime | None:
    resp = requests.get(
        f"{supabase_url}/rest/v1/schedule_heartbeats",
        headers={
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
        },
        params={
            "job": f"eq.{job}",
            "select": "ran_at",
            "order": "ran_at.desc",
            "limit": "1",
        },
        timeout=15,
    )
    resp.raise_for_status()
    rows = resp.json()
    if not rows:
        return None
    return datetime.fromisoformat(rows[0]["ran_at"].replace("Z", "+00:00"))


def check() -> list[str]:
    supabase_url = os.environ["SUPABASE_URL"]
    service_key = os.environ["SUPABASE_SERVICE_KEY"]
    now = datetime.now(timezone.utc)

    problems = []
    for job, max_gap_hours in EXPECTED_JOBS.items():
        last = _last_heartbeat(supabase_url, service_key, job)
        if last is None:
            problems.append(f"'{job}' — 기록이 아예 없음")
            continue
        gap = now - last
        if gap > timedelta(hours=max_gap_hours):
            hours = gap.total_seconds() / 3600
            problems.append(f"'{job}' — 마지막 실행 {hours:.0f}시간 전 (허용 {max_gap_hours}시간)")

    return problems


def notify(problems: list[str]) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    lines = ["🚨 FABOT 스케줄 워치독", "", "예정대로 안 돈 스케줄이 있습니다:"]
    lines += [f"- {p}" for p in problems]
    text = "\n".join(lines)
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text[:4000]},
        timeout=20,
    )
    resp.raise_for_status()


def main():
    problems = check()
    if not problems:
        print("모든 스케줄이 정상 범위 안에서 최근에 실행됨.")
        return
    print("문제 발견:")
    for p in problems:
        print(f"- {p}")
    notify(problems)


if __name__ == "__main__":
    main()
