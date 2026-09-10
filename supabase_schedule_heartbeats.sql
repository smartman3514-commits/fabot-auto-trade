-- 스케줄 감시(워치독)용 테이블. 각 스케줄이 "돌긴 돌았다"는 것만 매번 한 줄 남긴다
-- (매매/발송 여부와 무관하게 기록 — 조용히 아예 안 도는 것과 정상적으로 "관망/대기"인
-- 것을 구분하기 위함). 2026-09-10, 스케줄 감시 부재 문제 해결.

create table schedule_heartbeats (
  id bigint generated always as identity primary key,
  job text not null,               -- 'auto_trade_workflow', 'briefing_us_close' 등
  ran_at timestamptz not null default now(),
  detail text
);

create index schedule_heartbeats_job_ran_at_idx on schedule_heartbeats (job, ran_at desc);

alter table schedule_heartbeats enable row level security;

create policy "service role full access" on schedule_heartbeats
  for all
  using (true)
  with check (true);
