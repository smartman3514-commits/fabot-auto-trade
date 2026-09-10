"""오늘의 F&G 신호와 자동매매 실행 결과를 한국어 음성으로 요약해준다.

Main Quest 2("내 도메인에서 AI로 개선 지점 찾아서 PoC 만들기")의 AI 모델 요구사항을
자동매매 PoC 쪽에 적용한 것 — 음성 생성(edge-tts, ko-KR-SunHiNeural)을 배당 브리핑
PoC(dividend-tax-briefing)에서 이미 검증했던 그대로 재사용한다. 배당액처럼 개인정보가
드러나는 숫자 대신, 오늘의 매매 신호와 실행 결과만 말하므로 발표 자료로 그대로 보여줘도
된다.

모델 선정 근거는 dividend-tax-briefing/README.md와 동일 — 무료·API 키 불필요·신경망
기반이라 자연스러운 음성이라는 이유로 edge-tts를 그대로 재사용했다. 매번 새 모델을
고르는 대신 이미 검증된 모델을 재사용하는 것도 실무에서 흔한 선택이라 그대로 뒀다.
"""

import asyncio
from datetime import date
from pathlib import Path

VOICE = "ko-KR-SunHiNeural"
OUTPUT_DIR = Path(__file__).resolve().parent / "output"


def build_briefing_text(today_info: dict, raw, result: dict, outcome: dict) -> str:
    """today_info=today_signal.get_today_score(), raw=RawSignal, result=apply_cooldown() 반환값,
    outcome=자동실행 함수들이 돌려주는 dict({"executed": bool, ...})."""
    lines = [f"{today_info['date']} 기준, 오늘의 F&G 지수는 {today_info['score']:.1f}점입니다."]
    lines.append(f"판정 결과는 '{raw.label}'입니다.")

    if raw.action == "wait":
        lines.append("매수·매도 조건 밖이라 대기합니다.")
    elif result["cooldown"] and result["cooldown"]["in_cooldown"]:
        lines.append(f"매매 후 대기 기간이라 실행하지 않았습니다. {result['cooldown']['reason']}입니다.")
    elif outcome["executed"]:
        action_word = "매수" if outcome["action"] == "buy" else "매도"
        lines.append(
            f"{outcome['ticker']}를 {outcome['qty']}주 {action_word}해서 "
            f"{outcome['price']:,.2f} 가격에 체결했습니다."
        )
    else:
        lines.append(f"실행 조건은 맞았지만, {outcome['note']}")

    return " ".join(lines)


async def _synthesize(text: str, out_path: Path) -> None:
    import edge_tts

    communicate = edge_tts.Communicate(text, VOICE)
    await communicate.save(str(out_path))


async def synthesize_briefing_async(today_info: dict, raw, result: dict, outcome: dict) -> tuple[str, Path]:
    """텍스트를 만들고 음성 파일로 저장한 뒤 (텍스트, 저장경로)를 반환한다.
    이미 asyncio 이벤트 루프 안에 있는 호출부(auto_trade_loop.py)용."""
    text = build_briefing_text(today_info, raw, result, outcome)
    OUTPUT_DIR.mkdir(exist_ok=True)
    out_path = OUTPUT_DIR / f"trade_briefing_{date.today().strftime('%Y%m%d')}.mp3"
    await _synthesize(text, out_path)
    return text, out_path


def synthesize_briefing(today_info: dict, raw, result: dict, outcome: dict) -> tuple[str, Path]:
    """synthesize_briefing_async의 동기 버전 — 이벤트 루프 밖(스크립트 단독 실행 등)에서 씀."""
    return asyncio.run(synthesize_briefing_async(today_info, raw, result, outcome))
