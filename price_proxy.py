"""가격 데이터만으로 F&G 대체 지표를 계산합니다.

실제 CNN 지수가 없는 구간(2011년 이전)을 메우거나,
계산 방식 자체를 검증하는 데 씁니다.

5가지 구성 요소 (CNN의 실제 7개 지표 중, 무료 가격 데이터로 근사 가능한 것들):
- 모멘텀: QQQ 125일 이동평균 대비 괴리율
- 낙폭: QQQ 250일 최고가 대비 하락률
- VIX 수준: VIX가 최근 1년 평균보다 높은 정도 (공포 지표)
- 안전자산 선호: QQQ 20일 수익률 - 국채(IEF) 20일 수익률
- 정크본드 수요: 정크본드(HYG) 20일 수익률 - 우량채(LQD) 20일 수익률

★ 각 지표는 "최근 1년"만 기준으로 표준화합니다(rolling z-score) — 미래를 미리 아는
   lookahead bias를 피하기 위함입니다.

★ 5개 지표를 합치는 가중치는, 2011~2026년 실제 CNN 데이터에 회귀분석(OLS)으로
   맞춘 값입니다. 이 구간에서 실제값과의 상관계수 0.77 수준을 확인했습니다.

★★ 2020-09-18 이후는 CNN이 공개하는 나머지 3개 실제 지표(풋/콜 비율, 주가 강도,
   주가 폭)를 fetch_components.py로 직접 받아와 8개 지표 모델을 쓸 수 있습니다.
   같은 기간 기준으로 비교하면 상관계수가 0.735 → 0.840으로 오릅니다
   (compare_models.py 참고). 2020-09-18 이전 구간은 CNN도 이 3개 지표를
   과거분으로 안 주기 때문에 5개 지표 모델만 계산 가능합니다.
"""

import pandas as pd

ROLLING_WINDOW = 252  # 표준화 기준 기간 (거래일 기준 약 1년)

# 2011~2026 실제 CNN 데이터에 회귀분석으로 맞춘 가중치 (5개 지표, 전체 기간 계산 가능)
WEIGHTS = {
    "momentum": 0.3207,
    "drawdown": -0.1571,
    "vix_level": 0.4888,
    "safe_haven": 0.4119,
    "junk_demand": 0.1863,
}
INTERCEPT = -14.5007

# 2020-09-18~2026 실제 CNN 데이터에 회귀분석으로 맞춘 가중치 (8개 지표, 2020-09-18 이후만 계산 가능)
WEIGHTS_8 = {
    "momentum": 0.2991,
    "drawdown": -0.0545,
    "vix_level": 0.2641,
    "safe_haven": 0.2858,
    "junk_demand": 0.0788,
    "put_call": 5.3616,
    "strength": -1.1309,
    "breadth": 0.0273,
}
INTERCEPT_8 = -28.1170

# 2026-07-22: 실시간 파이프라인(live_fg.py, dashboard/scripts/update_live_score.py)이
# KIS API로 갈아타면서 VIX 현물지수 대신 VIXY(선물 ETF)를 쓰게 됨(KIS가 VIX 지수 자체는
# 안 줌). VIXY는 콘탱고 등으로 VIX와 통계적 성질이 달라서, 기존 WEIGHTS/WEIGHTS_8을
# 그대로 쓰면 실제값과 계속 벌어진다(관측: 실시간값이 CNN 공식보다 +10점 안팎 높게 나옴).
# fit_weights_vixy.py로 VIXY 기준 재회귀한 가중치. 5개 지표 모델은 상관계수 0.729로
# 기준(0.75) 미달이지만(VIXY의 한계), 실제 쓰이는 8개 지표 모델은 0.834로 원래(VIX) 모델의
# 0.840과 거의 동일 — CNN 실제 3개 지표가 VIX/VIXY 차이를 상당히 상쇄해준다.
WEIGHTS_VIXY = {
    "momentum": 0.2481,
    "drawdown": 0.1615,
    "vix_level": 0.1250,
    "safe_haven": 0.4653,
    "junk_demand": 0.1755,
}
INTERCEPT_VIXY = -11.3453

WEIGHTS_8_VIXY = {
    "momentum": 0.2616,
    "drawdown": 0.0901,
    "vix_level": 0.2107,
    "safe_haven": 0.3030,
    "junk_demand": 0.0511,
    "put_call": 2.2846,
    "strength": -1.3636,
    "breadth": 0.0291,
}
INTERCEPT_8_VIXY = -32.2342


def _rolling_zscore_to_0_100(series: pd.Series, invert: bool = False) -> pd.Series:
    rolling_mean = series.rolling(ROLLING_WINDOW, min_periods=60).mean()
    rolling_std = series.rolling(ROLLING_WINDOW, min_periods=60).std()
    z = (series - rolling_mean) / rolling_std
    if invert:
        z = -z
    return (50 + z * 15).clip(0, 100)


def compute_component_scores(
    qqq: pd.Series,
    vix: pd.Series,
    ief: pd.Series,
    hyg: pd.Series,
    lqd: pd.Series,
) -> pd.DataFrame:
    """가중치 적용 전, 5가지 지표 각각을 0~100으로 표준화한 값만 반환합니다.

    이 함수는 compute_price_based_fg()와 fit_weights.py(가중치를 재현하는
    회귀분석 스크립트)가 공유합니다 — 지표 계산 로직을 한 곳에서만 관리하기 위함.
    """
    prices = pd.DataFrame({"qqq": qqq, "vix": vix, "ief": ief, "hyg": hyg, "lqd": lqd})
    prices = prices.ffill()

    ma125 = prices["qqq"].rolling(125, min_periods=20).mean()
    momentum_score = _rolling_zscore_to_0_100((prices["qqq"] - ma125) / ma125)

    rolling_max = prices["qqq"].rolling(250, min_periods=20).max()
    drawdown_score = _rolling_zscore_to_0_100((prices["qqq"] - rolling_max) / rolling_max)

    vix_level_score = _rolling_zscore_to_0_100(prices["vix"], invert=True)

    safe_haven = prices["qqq"].pct_change(20) - prices["ief"].pct_change(20)
    safe_haven_score = _rolling_zscore_to_0_100(safe_haven)

    junk_demand = prices["hyg"].pct_change(20) - prices["lqd"].pct_change(20)
    junk_demand_score = _rolling_zscore_to_0_100(junk_demand)

    return pd.DataFrame(
        {
            "momentum": momentum_score,
            "drawdown": drawdown_score,
            "vix_level": vix_level_score,
            "safe_haven": safe_haven_score,
            "junk_demand": junk_demand_score,
        }
    )


def compute_price_based_fg(
    qqq: pd.Series,
    vix: pd.Series,
    ief: pd.Series,
    hyg: pd.Series,
    lqd: pd.Series,
    put_call: pd.Series = None,
    strength: pd.Series = None,
    breadth: pd.Series = None,
    weights: dict = None,
    intercept: float = None,
    weights_8: dict = None,
    intercept_8: float = None,
) -> pd.Series:
    """시장 데이터를 받아 0~100 F&G 대체 점수를 반환합니다.

    put_call/strength/breadth를 모두 넘기면(2020-09-18 이후만 가능) 8개 지표
    모델을 쓰고, 하나라도 없으면(과거 백테스트 등) 5개 지표 모델로 계산합니다.

    weights/intercept/weights_8/intercept_8을 넘기면 기본값(WEIGHTS/WEIGHTS_8, 진짜
    VIX로 회귀분석한 값) 대신 그 가중치를 쓴다 — vix 인자로 VIXY 등 VIX가 아닌 대체
    지표를 넘길 때는 WEIGHTS_VIXY/WEIGHTS_8_VIXY를 넘겨야 한다(live_fg.py 참고).
    """
    weights = weights or WEIGHTS
    intercept = INTERCEPT if intercept is None else intercept
    weights_8 = weights_8 or WEIGHTS_8
    intercept_8 = INTERCEPT_8 if intercept_8 is None else intercept_8

    components = compute_component_scores(qqq, vix, ief, hyg, lqd)

    if put_call is not None and strength is not None and breadth is not None:
        components = components.assign(put_call=put_call, strength=strength, breadth=breadth)
        fg_score = (
            weights_8["momentum"] * components["momentum"]
            + weights_8["drawdown"] * components["drawdown"]
            + weights_8["vix_level"] * components["vix_level"]
            + weights_8["safe_haven"] * components["safe_haven"]
            + weights_8["junk_demand"] * components["junk_demand"]
            + weights_8["put_call"] * components["put_call"]
            + weights_8["strength"] * components["strength"]
            + weights_8["breadth"] * components["breadth"]
            + intercept_8
        )
        return fg_score.clip(0, 100).round(2)

    fg_score = (
        weights["momentum"] * components["momentum"]
        + weights["drawdown"] * components["drawdown"]
        + weights["vix_level"] * components["vix_level"]
        + weights["safe_haven"] * components["safe_haven"]
        + weights["junk_demand"] * components["junk_demand"]
        + intercept
    )
    return fg_score.clip(0, 100).round(2)


if __name__ == "__main__":
    from datetime import datetime

    from price_fetcher import fetch_price_history

    start = datetime(2009, 1, 1)
    qqq = fetch_price_history("QQQ", start)
    vix = fetch_price_history("^VIX", start)
    ief = fetch_price_history("IEF", start)
    hyg = fetch_price_history("HYG", start)
    lqd = fetch_price_history("LQD", start)

    fg = compute_price_based_fg(qqq, vix, ief, hyg, lqd)
    print(fg.tail(10))
