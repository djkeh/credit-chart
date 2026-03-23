"""
신용거래융자 잔고 + 증권사 신용공여 한도 추이 차트
=====================================================
실행 방법:
    pip install plotly requests pandas openpyxl
    python credit_chart.py

결과:
    브라우저에서 credit_chart.html 자동 오픈

데이터 출처:
    - 신용거래융자 잔고   : 금융투자협회 FreeSIS (freesis.kofia.or.kr)
                           엔드포인트: /meta/getMetaDataList.do
                           서비스ID: STATSCU0100000070BO (TMPV2 필드, 백만원 단위)
    - 증권사 자기자본     : 금융감독원 DART 전자공시 API (opendart.fss.or.kr)
                           fnlttMultiAcnt.json 으로 10개사 동시 조회
                           (기존 120회 → 12회로 감축)
                           미설정 시 내장 fallback 추정치 자동 사용

DART API 호출 횟수:
    기존: 10개사 × 3년 × 4분기 = 120회 (fnlttSinglAcntAll, 회사별 개별 조회)
    개선: 3년 × 4분기 = 12회 (fnlttMultiAcnt, 최대 100개사 동시 조회)
"""

import os
import time
import warnings
import datetime as dt

import requests
import pandas as pd
import plotly.graph_objects as go

warnings.filterwarnings("ignore")

# ══════════════════════════════════════════════════════════════════════════════
# 설정
# ══════════════════════════════════════════════════════════════════════════════

# DART API 키 (https://opendart.fss.or.kr 에서 무료 발급)
DART_API_KEY = os.environ.get("DART_API_KEY", "")

# 차트 기간: 2024년 1월 1일 ~ 오늘
START_DATE = "2024-01-01"

# ── FreeSIS 설정 (브라우저 개발자도구로 검증된 실제 파라미터) ─────────────────
FREESIS_DATA_URL = "http://freesis.kofia.or.kr/meta/getMetaDataList.do"
FREESIS_PARAMS = {
    "OBJ_NM":  "STATSCU0100000070BO",
    "tmpV1":   "D",           # D=일별
    "tmpV40":  "1000000",     # 단위(백만원)
    "tmpV41":  "1",
    "tmpV45":  "{START_DATE}",
    "tmpV46":  "{END_DATE}",
}
DATA_JSON_PATH = ["ds1"]
DATE_FIELD     = "TMPV1"      # 날짜 (YYYYMMDD)
BALANCE_FIELD  = "TMPV2"      # 코스피+코스닥 신용융자 합산 (백만원)
BALANCE_UNIT   = 1e6          # 백만원 → 조원

# ── 한도선 계산 설정 ──────────────────────────────────────────────────────────
# DART 고유번호 (corp_code) — fnlttMultiAcnt API에서 콤마 구분으로 한 번에 조회
MAJOR_BROKERS = {
    "한국투자증권": "00160144",
    "NH투자증권":   "00120182",
    "미래에셋증권": "00111722",
    "삼성증권":     "00104088",
    "KB증권":       "00164876",
    "신한투자증권": "00138037",
    "하나증권":     "00115392",
    "키움증권":     "00296879",
    "메리츠증권":   "00141009",
    "대신증권":     "00108396",
}

LEGAL_CAP_RATIO   = 1.00   # 자본시장법: 종투사 자기자본 100%
MGMT_CAP_RATIO    = 0.60   # 업계 자체 관리 관행: ~60%
CREDIT_LOAN_SHARE = 0.50   # 신용공여 전체 한도 중 신용거래융자 배분 추정 비율
                            # 근거: 2026-03-04 실제 한도 소진(잔고 32.8조) 역산

OUTPUT_FILE = "index.html"


# ══════════════════════════════════════════════════════════════════════════════
# 데이터 수집
# ══════════════════════════════════════════════════════════════════════════════

def fetch_credit_balance(start_date: str, end_date: str) -> pd.DataFrame:
    """FreeSIS에서 신용거래융자 잔고를 수집합니다. (단위: 조원)"""
    s = start_date.replace("-", "")
    e = end_date.replace("-", "")

    payload = {"dmSearch": {
        k: v.replace("{START_DATE}", s).replace("{END_DATE}", e)
        for k, v in FREESIS_PARAMS.items()
    }}

    print(f"  [FreeSIS] {start_date} ~ {end_date} 조회 중...")
    resp = requests.post(FREESIS_DATA_URL, json=payload,
                         headers={"Content-Type": "application/json"},
                         timeout=30)
    resp.raise_for_status()

    data = resp.json()
    rows = data
    for key in DATA_JSON_PATH:
        rows = rows[key]

    if not rows:
        raise ValueError("FreeSIS 응답 데이터가 비어 있습니다.")

    records = []
    for row in rows:
        raw_date = str(row.get(DATE_FIELD, "")).strip()
        raw_bal  = str(row.get(BALANCE_FIELD, "0")).replace(",", "").strip()
        if not raw_date or raw_bal in ("", "-", "0", "None"):
            continue
        try:
            date    = pd.to_datetime(raw_date, format="%Y%m%d")
            balance = float(raw_bal) / BALANCE_UNIT
            records.append({"date": date, "balance": balance})
        except (ValueError, TypeError):
            continue

    if not records:
        raise ValueError(f"FreeSIS 파싱 결과 없음. 첫 행: {rows[0]}")

    df = (pd.DataFrame(records)
          .set_index("date")
          .sort_index()
          .pipe(lambda d: d[~d.index.duplicated(keep="last")]))

    print(f"  [FreeSIS] {len(df)}개 영업일. "
          f"최신: {df['balance'].iloc[-1]:.2f}조원 ({df.index[-1].date()})")
    return df


def fetch_dart_equity(api_key: str) -> pd.DataFrame:
    """
    DART fnlttSinglAcntAll API로 주요 종투사 자기자본을 수집합니다.

    호출 횟수 최적화:
      - 분기말이 오늘 기준 45일 이상 지나지 않은 분기는 스킵 (미공시)
      - 오늘 기준: 약 8개 분기 × 10개사 = 80회 (기존 120회에서 감축)
      - fnlttMultiAcnt 대신 fnlttSinglAcntAll을 사용하는 이유:
        fnlttMultiAcnt는 내부 처리 한도로 인해 10개사 중 4개사만 반환하는
        문제가 있어 합산값이 실제의 절반 수준으로 과소 집계됨.

    반환: DatetimeIndex(분기말), 컬럼 ['equity_sum'] (단위: 조원)
    """
    if not api_key:
        print("  [DART] API 키 없음 → fallback 사용")
        return _equity_fallback()

    quarter_to_month = {"11013": 3, "11012": 6, "11014": 9, "11011": 12}
    base_url   = "https://opendart.fss.or.kr/api"
    today      = dt.date.today()
    start_year = dt.date.fromisoformat(START_DATE).year
    years      = list(range(start_year, today.year + 1))

    # 조회할 (year, report_code, month, quarter_end) 목록 — 미공시 분기 선제 제외
    targets = []
    for year in years:
        for report_code, month in quarter_to_month.items():
            qend = (pd.Timestamp(year=year, month=month, day=1)
                    + pd.offsets.MonthEnd(0))
            # 분기말 후 45일 미만이면 아직 공시되지 않았을 가능성이 높음
            if (pd.Timestamp(today) - qend).days < 45:
                continue
            targets.append((year, report_code, month, qend))

    max_calls = len(targets) * len(MAJOR_BROKERS)
    print(f"  [DART] {len(MAJOR_BROKERS)}개사 × {len(targets)}분기 = "
          f"최대 {max_calls}회 호출 예정...")

    monthly: dict = {}
    call_count = 0

    for year, report_code, month, qend in targets:
        quarter_total = 0.0
        quarter_companies = 0

        for name, corp_code in MAJOR_BROKERS.items():
            try:
                r = requests.get(
                    f"{base_url}/fnlttSinglAcntAll.json",
                    params={
                        "crtfc_key":  api_key,
                        "corp_code":  corp_code,
                        "bsns_year":  str(year),
                        "reprt_code": report_code,
                        "fs_div":     "OFS",   # 별도재무제표
                    },
                    timeout=20
                )
                call_count += 1
                data = r.json()
                if data.get("status") != "000":
                    continue

                for item in data.get("list", []):
                    if item.get("account_nm") in ("자본총계", "자기자본"):
                        raw = str(item.get("thstrm_amount", "0"))
                        try:
                            val = float(raw.replace(",", "").replace("-", "0") or "0")
                            if val > 0:
                                quarter_total     += val / 1e12
                                quarter_companies += 1
                        except ValueError:
                            pass
                        break  # 한 회사당 자본총계는 하나면 충분

                time.sleep(0.12)  # DART rate limit (초당 약 8회)

            except Exception as exc:
                print(f"    [DART] {name} {year}Q{month//3} 실패: {exc}")
                continue

        if quarter_companies >= 5:   # 10개사 중 절반 이상 수집돼야 유효
            monthly[qend] = quarter_total
            print(f"    {qend.date()}  합산 {quarter_total:.1f}조원  ({quarter_companies}개사)")
        elif quarter_companies > 0:
            print(f"    {qend.date()}  {quarter_companies}개사만 수집 → 제외 "
                  f"(합산 {quarter_total:.1f}조, 10개사 미만)")

    print(f"  [DART] API 호출 {call_count}회 완료.")

    if not monthly:
        print("  [DART] 유효한 분기 없음 → fallback 사용")
        return _equity_fallback()

    df = pd.Series(monthly).sort_index().rename("equity_sum").to_frame()
    print(f"  [DART] 수집 완료: {len(df)}개 분기, "
          f"범위 {df['equity_sum'].min():.1f} ~ {df['equity_sum'].max():.1f}조원")
    return df

def _equity_fallback() -> pd.DataFrame:
    """
    DART API 미설정/실패 시 내장 추정치.
    보도 및 분기보고서 기반 상위 10개 종투사 자기자본 합산. (단위: 조원)
    """
    # 실제 공시된 분기까지만 포함. 미래 추정 포인트는 넣지 않음.
    # (보간 시 미래 방향으로 선형 외삽되어 한도선이 왜곡되는 것을 방지)
    data = {
        "2024-03-31": 60.0,
        "2024-06-30": 61.8,
        "2024-09-30": 62.7,
        "2024-12-31": 63.5,
        "2025-03-31": 67.0,
        "2025-06-30": 69.9,
        "2025-09-30": 73.0,
        # 2025-12-31 이후는 아직 공시 전 → ffill로 73.0 유지
    }
    print("  [Fallback] 내장 자기자본 추정치 사용 (2025-09-30 이후 ffill)")
    return (pd.Series({pd.Timestamp(k): v for k, v in data.items()})
            .rename("equity_sum").to_frame())


# ══════════════════════════════════════════════════════════════════════════════
# 계산
# ══════════════════════════════════════════════════════════════════════════════

def equity_to_daily(equity_df: pd.DataFrame,
                    date_range: pd.DatetimeIndex) -> pd.DataFrame:
    """
    분기별 자기자본을 일별로 보간합니다.

    보간 정책:
    - 실제 데이터 포인트 사이: 선형(time) 보간
    - 첫 포인트 이전 (bfill): 가장 오래된 분기값으로 채움
    - 마지막 포인트 이후 (ffill): 가장 최근 분기값으로 수평 유지
      → 아직 공시되지 않은 분기를 임의로 외삽하지 않음
    """
    # date_range 시작이 equity_df 첫 포인트보다 앞이면 bfill용 앵커 추가
    # date_range 끝은 추가하지 않음 → 마지막 실제값 이후 ffill 적용
    anchor_start = date_range[0]
    full_idx = equity_df.index.union([anchor_start])
    eq = (equity_df
          .reindex(full_idx)
          .interpolate(method="time")  # 포인트 사이 선형 보간
          .bfill())                    # 첫 포인트 이전 채움
    # date_range 전체로 reindex 후 ffill: 마지막 포인트 이후 수평 유지
    return eq.reindex(date_range).interpolate(method="time").ffill().bfill()


def compute_cap_lines(equity_daily: pd.DataFrame) -> pd.DataFrame:
    caps = pd.DataFrame(index=equity_daily.index)
    caps["legal_cap"] = (equity_daily["equity_sum"]
                         * LEGAL_CAP_RATIO * CREDIT_LOAN_SHARE)
    caps["mgmt_cap"]  = (equity_daily["equity_sum"]
                         * MGMT_CAP_RATIO  * CREDIT_LOAN_SHARE)
    return caps


def to_weekly(df: pd.DataFrame) -> pd.DataFrame:
    return df.resample("W-FRI").last().dropna(how="all")


# ══════════════════════════════════════════════════════════════════════════════
# HTML 후처리: 정보 카드·경고 박스·안내 문구를 Plotly HTML에 주입
# ══════════════════════════════════════════════════════════════════════════════

def _build_info_html(latest_eq: float, eq_source: str,
                     latest_bal: float, peak_bal: float, peak_date: str,
                     legal_now: float, mgmt_now: float) -> str:
    """
    Plotly가 생성한 HTML에 삽입할 추가 정보 블록을 반환합니다.
    - 경고 박스 (신규 대출 중단 안내)
    - 4개 정보 카드 (법적 근거 / 자기자본 합산 / 자체 관리선 / 실질 한도)
    - 안내 문구
    """
    return f"""
<div style="max-width:1200px; margin:0 auto; padding:0 20px 24px; font-family: Malgun Gothic, Apple SD Gothic Neo, sans-serif;">

  <!-- 경고 박스 -->
  <div style="border-left:4px solid #e24b4a; padding:10px 14px; margin:0 0 14px;
              border-radius:0 6px 6px 0; background:#fff5f5;
              font-size:13px; color:#333; line-height:1.7;">
    ⚠ <b>2026년 3월 4일:</b>
    <b>한국투자증권 · NH투자증권 · 신한투자증권 · 카카오페이증권</b> 등
    신용거래융자 신규 매수 일시 중단 —
    잔고 32조원 돌파, 각 사 자기자본 100% 한도 소진 여파
  </div>

  <!-- 정보 카드 4개 -->
  <div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr));
              gap:10px; margin-bottom:14px;">

    <div style="background:#f8f8f8; border-radius:8px; padding:12px 16px;">
      <p style="font-size:11px; color:#888; margin:0 0 4px;">법적 근거</p>
      <p style="font-size:15px; font-weight:500; color:#e24b4a; margin:0;">자본시장법</p>
      <p style="font-size:11px; color:#888; margin:4px 0 0;">
        종투사 신용공여 합계 ≤ 자기자본 100%
      </p>
    </div>

    <div style="background:#f8f8f8; border-radius:8px; padding:12px 16px;">
      <p style="font-size:11px; color:#888; margin:0 0 4px;">
        상위 10개 종투사 자기자본 합산
      </p>
      <p style="font-size:15px; font-weight:500; color:#3266ad; margin:0;">
        ~{latest_eq:.0f}조원
      </p>
      <p style="font-size:11px; color:#888; margin:4px 0 0;">
        출처: {eq_source} · 신용융자 외 기업신용공여 등 포함
      </p>
    </div>

    <div style="background:#f8f8f8; border-radius:8px; padding:12px 16px;">
      <p style="font-size:11px; color:#888; margin:0 0 4px;">업계 자체 관리선 (관행)</p>
      <p style="font-size:15px; font-weight:500; color:#ef9f27; margin:0;">
        자기자본 ~{MGMT_CAP_RATIO*100:.0f}%
      </p>
      <p style="font-size:11px; color:#888; margin:4px 0 0;">
        현재 추정: {mgmt_now:.1f}조원 · 신용융자만 기준이면 더 낮을 수 있음
      </p>
    </div>

    <div style="background:#f8f8f8; border-radius:8px; padding:12px 16px;">
      <p style="font-size:11px; color:#888; margin:0 0 4px;">실질 신용융자 한도 (추정)</p>
      <p style="font-size:15px; font-weight:500; color:#e24b4a; margin:0;">
        ~33~35조원
      </p>
      <p style="font-size:11px; color:#888; margin:4px 0 0;">
        3/4 중단 시점 기준 역산 추정 · 현재 법적 한도: {legal_now:.1f}조원
      </p>
    </div>

  </div>

  <!-- 안내 문구 -->
  <p style="font-size:11px; color:#999; margin:0; line-height:1.7;">
    ※ 법적 한도는 신용융자 단독이 아닌 전체 신용공여(기업대출·담보대출 포함) 기준.
    증권사마다 각사 자기자본의 100%가 개별 한도이며,
    한도선은 업계 전체를 추정한 참고값.
    신용공여 중 신용융자 배분 비율 {CREDIT_LOAN_SHARE*100:.0f}% 가정
    (2026-03-04 실제 한도 소진 역산).
    최신 잔고: <b>{latest_bal:.2f}조원</b> /
    기간 최고치: <b>{peak_bal:.2f}조원</b> ({peak_date})
  </p>

</div>
"""


def save_html_with_extras(fig: go.Figure,
                           info_html: str,
                           output_file: str) -> None:
    """
    Plotly 차트 HTML을 저장한 뒤,
    </body> 직전에 정보 블록 HTML을 삽입합니다.
    """
    raw_html = fig.to_html(
        include_plotlyjs="cdn",
        full_html=True,
        config={"scrollZoom": True, "displayModeBar": True},
    )
    # </body> 직전에 정보 블록 삽입
    final_html = raw_html.replace("</body>", info_html + "\n</body>")
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(final_html)


# ══════════════════════════════════════════════════════════════════════════════
# 차트 생성
# ══════════════════════════════════════════════════════════════════════════════

EVENTS = [
    ("2025-06-03", "이재명 정부 출범",                   +2.5, "#2d8a55"),
    ("2025-08-01", "세제개편안 충격",                     -2.5, "#ba7517"),
    ("2025-11-05", "역대 최고 경신<br>(25.8조, 11/5)",   +3.0, "#185fa5"),
    ("2026-01-06", "잔고 27.8조 신고점",                 +2.5, "#185fa5"),
    ("2026-03-04", "대형 증권사<br>신규 대출 중단",       -3.0, "#a32d2d"),
]


def build_chart(weekly_balance: pd.DataFrame,
                weekly_caps: pd.DataFrame,
                fetch_date: str) -> go.Figure:

    fig = go.Figure()

    # ── 위험 구간 배경 밴드 ────────────────────────────────────────────────────
    fig.add_trace(go.Scatter(
        x=list(weekly_caps.index) + list(weekly_caps.index[::-1]),
        y=list(weekly_caps["legal_cap"].round(2))
          + list(weekly_caps["mgmt_cap"].round(2)[::-1]),
        fill="toself",
        fillcolor="rgba(226,75,74,0.07)",
        line=dict(width=0),
        hoverinfo="skip",
        showlegend=False,
        name="_band",
    ))

    # ── 법적 한도선 ───────────────────────────────────────────────────────────
    fig.add_trace(go.Scatter(
        x=weekly_caps.index,
        y=weekly_caps["legal_cap"].round(1),
        mode="lines",
        name=(f"법적 한도 (자기자본 100% × "
              f"신용융자 배분 {CREDIT_LOAN_SHARE*100:.0f}%, 추정)"),
        line=dict(color="#e24b4a", width=1.8, dash="dash"),
        hovertemplate="%{x|%Y-%m-%d}<br>법적 한도: <b>%{y:.1f}조원</b><extra></extra>",
    ))

    # ── 업계 자체 관리선 ──────────────────────────────────────────────────────
    fig.add_trace(go.Scatter(
        x=weekly_caps.index,
        y=weekly_caps["mgmt_cap"].round(1),
        mode="lines",
        name=(f"업계 자체 관리선 (자기자본 "
              f"{MGMT_CAP_RATIO*100:.0f}% × 신용융자 배분 {CREDIT_LOAN_SHARE*100:.0f}%, 추정)"),
        line=dict(color="#ef9f27", width=1.6, dash="dot"),
        hovertemplate="%{x|%Y-%m-%d}<br>자체 관리선: <b>%{y:.1f}조원</b><extra></extra>",
    ))

    # ── 신용거래융자 잔고 ─────────────────────────────────────────────────────
    fig.add_trace(go.Scatter(
        x=weekly_balance.index,
        y=weekly_balance["balance"].round(2),
        mode="lines+markers",
        name="신용거래융자 잔고 (코스피+코스닥)",
        line=dict(color="#3266ad", width=2.2),
        marker=dict(size=4, color="#3266ad"),
        fill="tozeroy",
        fillcolor="rgba(50,102,173,0.10)",
        hovertemplate="%{x|%Y-%m-%d}<br>잔고: <b>%{y:.2f}조원</b><extra></extra>",
    ))

    # ── 이벤트 수직선 + 어노테이션 ───────────────────────────────────────────
    for event_date, label, y_off, color in EVENTS:
        ts = pd.Timestamp(event_date)
        if ts < weekly_balance.index[0] or ts > weekly_balance.index[-1]:
            continue

        fig.add_vline(
            x=ts.timestamp() * 1000,
            line=dict(color=color, width=1.2, dash="dot"),
        )

        idx     = weekly_balance.index.get_indexer([ts], method="nearest")[0]
        y_base  = weekly_balance["balance"].iloc[idx]

        fig.add_annotation(
            x=ts,
            y=y_base + y_off,
            text=f"<b>{label}</b>",
            showarrow=True,
            arrowhead=2,
            arrowsize=0.8,
            arrowcolor=color,
            arrowwidth=1.2,
            ax=0,
            ay=-28,
            font=dict(size=10, color=color),
            bgcolor="rgba(255,255,255,0.90)",
            bordercolor=color,
            borderwidth=1,
            borderpad=4,
            xanchor="center",
        )

    # ── 위험 구간 레이블 ──────────────────────────────────────────────────────
    # 잔고가 자체관리선을 넘어선 시점의 중간 x값에 표시
    over_mgmt = weekly_balance[
        weekly_balance["balance"] > weekly_caps["mgmt_cap"]
    ]
    if not over_mgmt.empty:
        mid_idx  = len(over_mgmt) // 2
        mid_ts   = over_mgmt.index[mid_idx]
        mid_caps = weekly_caps.loc[mid_ts, "mgmt_cap"] if mid_ts in weekly_caps.index \
                   else weekly_caps["mgmt_cap"].mean()
        fig.add_annotation(
            x=mid_ts,
            y=mid_caps + 2.5,
            text="<b>위험 구간</b><br><span style='font-size:9px'>(잔고 > 자체관리선)</span>",
            showarrow=False,
            font=dict(size=10, color="#854f0b"),
            bgcolor="rgba(255,243,220,0.88)",
            bordercolor="#ef9f27",
            borderwidth=1,
            borderpad=4,
        )

    # ── 신규 대출 중단 포인트 마커 ────────────────────────────────────────────
    shutdown_ts = pd.Timestamp("2026-03-04")
    if weekly_balance.index[0] <= shutdown_ts <= weekly_balance.index[-1]:
        idx_s = weekly_balance.index.get_indexer([shutdown_ts], method="nearest")[0]
        sv    = weekly_balance["balance"].iloc[idx_s]
        fig.add_trace(go.Scatter(
            x=[weekly_balance.index[idx_s]],
            y=[sv],
            mode="markers",
            marker=dict(size=13, color="#e24b4a", symbol="circle",
                        line=dict(width=2.5, color="white")),
            name="신규 대출 중단 (2026-03-04)",
            hovertemplate=(f"2026-03-04<br>신규 대출 중단<br>"
                           f"잔고: <b>{sv:.2f}조원</b><extra></extra>"),
        ))

    # ── 레이아웃 ──────────────────────────────────────────────────────────────
    y_max = max(
        weekly_balance["balance"].max(),
        weekly_caps["legal_cap"].max()
    ) * 1.18   # 어노테이션 공간 확보를 위해 여유 충분히

    fig.update_layout(
        title=dict(
            text=(
                "개인 신용거래융자 잔고 + 증권사 신용공여 한도 추이<br>"
                f"<sup>주간 단위 | 데이터 기준일: {fetch_date} | "
                "출처: 금융투자협회 FreeSIS · 금융감독원 DART</sup>"
            ),
            font=dict(size=17),
            x=0.5,
        ),
        xaxis=dict(
            title="",
            tickformat="%y/%m",
            tickangle=-35,
            showgrid=True,
            gridcolor="rgba(0,0,0,0.06)",
            rangeslider=dict(
                visible=True,
                thickness=0.10,      # ← 슬라이더 세로 폭 (0.04 → 0.10, 약 2.5배)
                bgcolor="rgba(50,102,173,0.06)",
            ),
        ),
        yaxis=dict(
            title="잔고 (조원)",
            ticksuffix="조",
            showgrid=True,
            gridcolor="rgba(0,0,0,0.06)",
            range=[12, y_max],
            zeroline=False,
        ),
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="left",
            x=0,
            font=dict(size=11),
            bgcolor="rgba(255,255,255,0.85)",
        ),
        hovermode="x unified",
        plot_bgcolor="white",
        paper_bgcolor="white",
        font=dict(
            family="Malgun Gothic, Apple SD Gothic Neo, sans-serif",
            color="#333",
        ),
        # 하단 여백 최소화: 정보 블록은 HTML 주입 방식으로 처리
        margin=dict(t=130, b=30, l=65, r=40),
        height=660,
    )

    return fig


# ══════════════════════════════════════════════════════════════════════════════
# 메인
# ══════════════════════════════════════════════════════════════════════════════

def main():
    today     = dt.date.today()
    start_str = START_DATE
    end_str   = today.strftime("%Y-%m-%d")

    print("=" * 55)
    print("  신용거래융자 잔고 + 한도선 차트 생성")
    print(f"  기간: {start_str} ~ {end_str}")
    print("=" * 55)

    # 1) 잔고 수집
    print("\n[1/4] 신용거래융자 잔고 수집 (FreeSIS)...")
    balance_df = fetch_credit_balance(start_str, end_str)

    # 2) 자기자본 수집
    print("\n[2/4] 증권사 자기자본 수집 (DART)...")
    masked_key = f"{DART_API_KEY[:6]}****" if DART_API_KEY else "없음"
    print(f"  -- 사용 키: {masked_key}")
    equity_df = fetch_dart_equity(DART_API_KEY)
    dart_used = (DART_API_KEY != "" and len(equity_df) > 1)
    eq_source = "DART API" if dart_used else "내장 추정치"

    # 3) 한도선 계산
    print("\n[3/4] 한도선 계산...")
    equity_daily = equity_to_daily(equity_df, balance_df.index)
    caps_df      = compute_cap_lines(equity_daily)

    print(f"  자기자본 범위  : {equity_daily['equity_sum'].min():.1f}"
          f" ~ {equity_daily['equity_sum'].max():.1f}조원")
    print(f"  법적 한도 범위 : {caps_df['legal_cap'].min():.1f}"
          f" ~ {caps_df['legal_cap'].max():.1f}조원")
    print(f"  자체 관리선 범위: {caps_df['mgmt_cap'].min():.1f}"
          f" ~ {caps_df['mgmt_cap'].max():.1f}조원")

    # 4) 차트 생성 및 저장
    print("\n[4/4] 주간 집계 및 차트 생성...")
    weekly_balance = to_weekly(balance_df)
    weekly_caps    = to_weekly(caps_df)

    latest    = balance_df["balance"].iloc[-1]
    peak      = balance_df["balance"].max()
    peak_date = balance_df["balance"].idxmax().strftime("%Y-%m-%d")
    legal_now = caps_df["legal_cap"].iloc[-1]
    mgmt_now  = caps_df["mgmt_cap"].iloc[-1]
    latest_eq = equity_df["equity_sum"].iloc[-1]

    print("\n  ──── 요약 ────────────────────────────────────")
    print(f"  최신 잔고        : {latest:.2f}조원 ({balance_df.index[-1].date()})")
    print(f"  기간 최고치      : {peak:.2f}조원 ({peak_date})")
    print(f"  현재 법적 한도   : {legal_now:.1f}조원 (추정)")
    print(f"  현재 자체 관리선 : {mgmt_now:.1f}조원 (추정)")
    print(f"  한도 소진율      : {latest / legal_now * 100:.1f}%")
    print(f"  종투사 자기자본  : {latest_eq:.1f}조원 ({eq_source})")
    print("  ──────────────────────────────────────────────")

    fig       = build_chart(weekly_balance, weekly_caps,
                            today.strftime("%Y-%m-%d"))
    info_html = _build_info_html(latest_eq, eq_source,
                                 latest, peak, peak_date,
                                 legal_now, mgmt_now)
    save_html_with_extras(fig, info_html, OUTPUT_FILE)

    print(f"\n  차트 저장 완료: {OUTPUT_FILE}")
    import webbrowser
    webbrowser.open(f"file://{os.path.abspath(OUTPUT_FILE)}")
    print("  브라우저에서 자동으로 열립니다.\n")


if __name__ == "__main__":
    main()
