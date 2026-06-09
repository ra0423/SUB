# -*- coding: utf-8 -*-
"""
노인 무임승차 제도 개선 — 데이터 기반 정책 어드바이저
=====================================================
4가지 기능:
  ① 노인 다이용 역 지도 (노인 아이콘 시각화)
  ② 다이용 역 주변 시설 정보
  ③ 시간대별 승차·하차 핫스팟 지도
  ④ 호선·시간대·요금할인별 적자(무임손실) 회복 시뮬레이터

데이터: 같은 폴더의 subway_data.db (SQLite). 별도 DB 서버 불필요.
지도: folium(OpenStreetMap) — API 키 불필요.
AI 자문(선택): Gemini API 키가 있으면 분석 결과로 정책 제언 생성.
"""

import os
import re
import sqlite3

import pandas as pd
import streamlit as st
import plotly.express as px
import folium
from streamlit_folium import st_folium

# ──────────────────────────────────────────────────────────────────────────
# 0. 기본 설정 / 상수
# ──────────────────────────────────────────────────────────────────────────
DB_PATH = os.environ.get("SUBWAY_DB_PATH", "subway_data.db")
GEMINI_MODEL = "gemini-2.5-flash"
BASE_FARE_DEFAULT = 1550          # 2025-06-28 인상된 수도권 지하철 카드 기본요금(성인, 10km 이내)
SEOUL_CENTER = [37.5563, 126.9905]

st.set_page_config(page_title="노인 무임승차 정책 어드바이저", page_icon="🚇", layout="wide")

# 시간대(시) 정의 — 하루 전체를 4구간으로 분할(합치면 전체)
BANDS = {
    "출근피크(7-9시)":  [7, 8, 9],
    "낮시간(10-17시)":  [10, 11, 12, 13, 14, 15, 16, 17],
    "퇴근피크(18-20시)": [18, 19, 20],
    "그외(6·21-25시)":  [6, 21, 22, 23, 24, 25],
}
def _sum_expr(hours):  # ["6시"+...] SQL 식 (숫자로 시작하는 컬럼명은 큰따옴표 필수)
    return "+".join(f'"{h}시"' for h in hours)

H_ALL = _sum_expr(range(6, 26))

# 역번호 → 호선 교정 매핑.
# 주의: 원본 노인이용 테이블에 (잘못된) '호선' 컬럼이 이미 있어,
#       별칭을 '호선'으로 두면 GROUP BY가 원본 컬럼을 잡는다. 반드시 '노선' 등 다른 이름을 쓸 것.
LINE_CASE = """CASE
  WHEN 역번호 BETWEEN 100 AND 199 THEN '1호선'
  WHEN 역번호 BETWEEN 200 AND 299 THEN '2호선'
  WHEN 역번호 BETWEEN 300 AND 399 THEN '3호선'
  WHEN 역번호 BETWEEN 400 AND 499 THEN '4호선'
  WHEN 역번호 BETWEEN 2500 AND 2599 THEN '5호선'
  WHEN 역번호 BETWEEN 2600 AND 2699 THEN '6호선'
  WHEN 역번호 BETWEEN 2700 AND 2799 THEN '7호선'
  WHEN 역번호 BETWEEN 2800 AND 2899 THEN '8호선'
  ELSE '기타' END"""


# ──────────────────────────────────────────────────────────────────────────
# 1. 데이터 접근 (캐싱)
# ──────────────────────────────────────────────────────────────────────────
@st.cache_data(show_spinner=False)
def run_query(sql: str) -> pd.DataFrame:
    con = sqlite3.connect(DB_PATH)
    try:
        return pd.read_sql_query(sql, con)
    finally:
        con.close()


def _norm(name) -> str:
    """역명 정규화: 괄호 설명·끝의 '역' 제거 → 좌표 테이블 매칭률 향상."""
    if not isinstance(name, str):
        return ""
    s = re.sub(r"\(.*?\)", "", name).strip()
    if len(s) > 1 and s.endswith("역"):
        s = s[:-1]
    return s


@st.cache_data(show_spinner=False)
def coord_map() -> dict:
    """정규화 역명 → (위도, 경도) 사전. 원본 역좌표 + 누락 주요역 보완좌표."""
    c = run_query("SELECT 역명, 위도, 경도 FROM 역좌표 WHERE 역명 IS NOT NULL")
    c["key"] = c["역명"].map(_norm)
    c = c.drop_duplicates("key")
    cm = {r.key: (r.위도, r.경도) for r in c.itertuples()}
    # 원본 역좌표에 없는 노인 다이용 주요역의 근사 좌표 보완(시각화용·근사치)
    SUPPLEMENT = {
        "청량리": (37.5803, 127.0469), "잠실": (37.5133, 127.1001),
        "회현": (37.5585, 126.9784),  "수유": (37.6380, 127.0254),
        "총신대입구": (37.4866, 126.9817), "서울대입구": (37.4813, 126.9527),
        "교대": (37.4934, 127.0145),  "천호": (37.5385, 127.1237),
        "양재": (37.4847, 127.0343),  "대림": (37.4927, 126.8956),
    }
    for k, v in SUPPLEMENT.items():
        cm.setdefault(k, v)
    return cm


def attach_coords(df: pd.DataFrame, name_col="역명") -> pd.DataFrame:
    """역명 기준으로 위도/경도 컬럼을 붙이고 숫자형으로 변환."""
    cm = coord_map()
    df = df.copy()
    keys = df[name_col].map(_norm)
    df["lat"] = pd.to_numeric(keys.map(lambda k: cm.get(k, (None, None))[0]), errors="coerce")
    df["lon"] = pd.to_numeric(keys.map(lambda k: cm.get(k, (None, None))[1]), errors="coerce")
    return df


@st.cache_data(show_spinner=False)
def senior_by_station() -> pd.DataFrame:
    """역별 연간 노인 이용(승차+하차 전체)."""
    return run_query(
        f"SELECT 역명, 역번호, SUM({H_ALL}) AS 노인_연간이용 "
        f"FROM 노인이용 GROUP BY 역번호 ORDER BY 노인_연간이용 DESC"
    )


@st.cache_data(show_spinner=False)
def board_alight_by_band() -> pd.DataFrame:
    """역별·승하차별 시간대 합계 (지도용)."""
    cols = ", ".join(f"SUM({_sum_expr(hs)}) AS \"{b}\"" for b, hs in BANDS.items())
    return run_query(
        f"SELECT 역번호, 역명, 승하차, {cols} FROM 노인이용 GROUP BY 역번호, 승하차"
    )


@st.cache_data(show_spinner=False)
def facility_summary() -> dict:
    """역번호 → (분류 요약 문자열, 예시 시설명). 지도 팝업용."""
    f = facilities()
    f = f[f["분류"] != "기타"]
    out = {}
    for sid, g in f.groupby("역번호"):
        cnt = g["분류"].value_counts()
        cats = " · ".join(f"{k} {v}" for k, v in cnt.items())
        examples = ", ".join(g["역주변"].head(4).astype(str).tolist())
        out[int(sid)] = (cats, examples)
    return out


@st.cache_data(show_spinner=False)
def facilities() -> pd.DataFrame:
    """역별 주변시설 원자료 + 분류."""
    df = run_query("SELECT 역번호, 역명, 역주변 FROM 역세권")
    def cat(x):
        x = x or ""
        if any(k in x for k in ("병원", "의원", "보건")):       return "의료"
        if any(k in x for k in ("시장", "전통")):              return "전통시장"
        if any(k in x for k in ("복지", "경로")):              return "복지시설"
        if any(k in x for k in ("공원", "관광")):              return "공원·여가"
        if any(k in x for k in ("학교", "대학")):              return "교육"
        if any(k in x for k in ("관공서", "구청", "주민센터")): return "행정"
        return "기타"
    df["분류"] = df["역주변"].map(cat)
    return df


@st.cache_data(show_spinner=False)
def boardings_line_band() -> pd.DataFrame:
    """[시뮬레이터 핵심] 호선 × 시간대 노인 '승차'(무임 건수) 합계."""
    cols = ", ".join(f"SUM({_sum_expr(hs)}) AS \"{b}\"" for b, hs in BANDS.items())
    # GROUP BY는 반드시 '노선'(교정 별칭)으로! '호선'은 원본 버그 컬럼이 잡힘
    return run_query(
        f"SELECT ({LINE_CASE}) AS 노선, {cols} "
        f"FROM 노인이용 WHERE 승하차='승차' GROUP BY 노선 ORDER BY 노선"
    )


# ──────────────────────────────────────────────────────────────────────────
# 2. Gemini AI 자문 (선택)
# ──────────────────────────────────────────────────────────────────────────
def get_api_key():
    try:
        if "GEMINI_API_KEY" in st.secrets:
            return st.secrets["GEMINI_API_KEY"]
    except Exception:
        pass
    return os.environ.get("GEMINI_API_KEY") or st.session_state.get("manual_api_key")


def generate_advice(api_key: str, context: str) -> str:
    from google import genai
    client = genai.Client(api_key=api_key)
    prompt = f"""당신은 대중교통 정책 데이터 분석가입니다. 아래는 서울 지하철 노인
무임승차·혼잡·역세권 데이터에서 도출한 실제 수치입니다.

{context}

이 수치에 근거해 '노인 무임승차 제도 개선' 자문 초안을 작성하세요.
구성: 1)핵심 진단 2)정책 대안(시간대 차등·노선 차등·부분 요금 등 2~3가지의 장단점)
3)우선 개입 대상 4)유의점(효율성과 복지·형평성 균형).
특정 입장을 단정하지 말고 의사결정자가 판단하도록 선택지와 근거를 제시하세요.
무임손실 추정은 '행동변화를 가정하지 않은 명목 최대치'임을 분명히 언급하세요.
한국어, 마크다운으로."""
    return client.models.generate_content(model=GEMINI_MODEL, contents=prompt).text


# ──────────────────────────────────────────────────────────────────────────
# 3. 화면
# ──────────────────────────────────────────────────────────────────────────
def main():
    if not os.path.exists(DB_PATH):
        st.error(f"DB 파일 `{DB_PATH}` 을(를) 찾을 수 없습니다. app.py와 같은 위치에 두세요.")
        st.stop()

    with st.sidebar:
        st.header("🚇 메뉴")
        st.caption("서울 지하철 1~8호선 · 365일 데이터")
        st.divider()
        st.subheader("🤖 Gemini API (선택)")
        if get_api_key():
            st.success("API 키 연결됨")
        else:
            st.info("AI 자문용. 없어도 ①~③ 기능은 작동합니다.")
            k = st.text_input("Gemini API Key", type="password")
            if k:
                st.session_state["manual_api_key"] = k
                st.rerun()
        st.divider()
        st.caption("데이터 기반 **참고용** 도구입니다. 정책 결정은 추가 검토가 필요합니다.")

    st.title("🚇 노인 무임승차 제도 개선 어드바이저")

    tabs = st.tabs([
        "① 노인 다이용 역 지도", "② 시간대별 승하차·주변시설",
        "③ 적자 회복 시뮬레이터", "🤖 AI 자문",
    ])

    # ── ① 노인 다이용 역 지도 (노인 아이콘) ──────────────────────────────
    with tabs[0]:
        st.subheader("① 노인이 많이 이용하는 역 — 지도 위 노인 아이콘")
        ss = attach_coords(senior_by_station())
        n = st.slider("표시할 상위 역 수", 5, 40, 20, key="t1n")
        top = ss.head(n)
        mapped = top.dropna(subset=["lat", "lon"])

        m = folium.Map(location=SEOUL_CENTER, zoom_start=11, tiles="CartoDB positron")
        vmax = mapped["노인_연간이용"].max() if not mapped.empty else 1
        for _, r in mapped.iterrows():
            v = int(r["노인_연간이용"])
            size = int(18 + 26 * (v / vmax))                  # 18~44px
            folium.Marker(
                [r["lat"], r["lon"]],
                tooltip=f"{r['역명']}: {v:,}",
                popup=folium.Popup(f"<b>{r['역명']}</b><br>연간 노인이용 {v:,}", max_width=200),
                icon=folium.DivIcon(
                    html=f'<div style="font-size:{size}px; line-height:1; text-align:center;">👴</div>',
                    icon_size=(size, size), icon_anchor=(size // 2, size // 2),
                ),
            ).add_to(m)
        st_folium(m, height=520, returned_objects=[], key="map1")

        c1, c2 = st.columns([3, 1])
        c1.caption("👴 아이콘이 클수록 노인 이용이 많은 역입니다.")
        miss = int(top["lat"].isna().sum())
        if miss:
            c2.caption(f"※ 좌표 없는 역 {miss}곳 제외")
        st.dataframe(top[["역명", "노인_연간이용"]], hide_index=True, use_container_width=True,
                     column_config={"노인_연간이용": st.column_config.NumberColumn(format="%d")})

    # ── ② 시간대별 승하차 + 주변시설 (지도 팝업) ─────────────────────────
    with tabs[1]:
        st.subheader("② 시간대별로 어디서 타고 내리나 + 역 주변시설")
        ba = board_alight_by_band()
        facsum = facility_summary()
        cc1, cc2 = st.columns(2)
        band = cc1.selectbox("시간대", list(BANDS.keys()), key="t2band")
        mode = cc2.radio("표시", ["승차+하차", "승차만", "하차만"], horizontal=True, key="t2mode")
        topk = st.slider("시간대별 상위 역 수(각 방향)", 5, 30, 15, key="t2n")

        # 역별로 승차/하차 값을 한 행에 모으고 좌표 부착
        name_map = ba.drop_duplicates("역번호").set_index("역번호")["역명"]
        pv = (ba.pivot_table(index="역번호", columns="승하차", values=band, aggfunc="sum")
                .fillna(0).reset_index())
        for col in ("승차", "하차"):
            if col not in pv.columns:
                pv[col] = 0
        pv["역명"] = pv["역번호"].map(name_map)
        pv = attach_coords(pv, "역명")

        def popup_html(row):
            sid = int(row["역번호"])
            cats, ex = facsum.get(sid, ("등록된 주변시설 정보 없음", ""))
            html = (f"<b>{row['역명']}</b><br>"
                    f"🔵 승차 {int(row['승차']):,} / 🟠 하차 {int(row['하차']):,}<br>"
                    f"<span style='color:#444'>🏥 주변시설: {cats}</span>")
            if ex:
                html += f"<br><span style='font-size:11px;color:#888'>예) {ex}</span>"
            return folium.Popup(html, max_width=260)

        m = folium.Map(location=SEOUL_CENTER, zoom_start=11, tiles="CartoDB positron")
        legend, plans = [], []
        if mode in ("승차+하차", "승차만"):
            plans.append(("승차", "#1f77b4", "승차(타는 곳)"))
        if mode in ("승차+하차", "하차만"):
            plans.append(("하차", "#e67e22", "하차(내리는 곳)"))
        for dir_, color, label in plans:
            d = pv.dropna(subset=["lat", "lon"]).sort_values(dir_, ascending=False).head(topk)
            vmax = d[dir_].max() if not d.empty else 1
            for _, r in d.iterrows():
                v = int(r[dir_])
                if v <= 0:
                    continue
                folium.CircleMarker(
                    [r["lat"], r["lon"]], radius=5 + 16 * (v / vmax),
                    color=color, fill=True, fill_color=color, fill_opacity=0.6, weight=1,
                    tooltip=f"[{label}] {r['역명']}: {v:,}",
                    popup=popup_html(r),
                ).add_to(m)
            legend.append((label, color))
        st_folium(m, height=520, returned_objects=[], key="map2")
        st.caption(" / ".join(f"● {lab}" for lab, _ in legend) +
                   f"  —  원 크기 = 해당 시간대 인원 · **마커를 클릭하면 주변시설이 표시**됩니다 ({band})")
        st.warning(
            "이 데이터는 역별 **승차/하차 총량**입니다. 'A역→B역'처럼 개별 이동을 연결한 "
            "기종점(OD) 자료가 아니므로, 출발·도착의 **공간 패턴**으로 해석해 주세요."
        )

    # ── ③ 적자 회복 시뮬레이터 (시간대별 요금 + 전후 비교) ────────────────
    with tabs[2]:
        st.subheader("③ 시간대별 요금을 다르게 매기면 무임손실이 얼마나 회복될까")
        bl = boardings_line_band()
        band_cols = list(BANDS.keys())

        base_fare = st.number_input(
            "기준(성인) 요금 — 무임손실 산정 기준(원)", 0, 5000, BASE_FARE_DEFAULT, 50,
            help="2025-06-28 인상된 수도권 카드 기본요금(성인)은 1,550원입니다.")
        lines = st.multiselect("적용 호선", bl["노선"].tolist(),
                               default=bl["노선"].tolist(), key="t3line")

        st.markdown("**시간대별 노인 부담 요금(원)** — 시나리오를 직접 설정하세요 (0 = 무료)")
        cols = st.columns(len(band_cols))
        defaults = [round(base_fare / 2), 0, round(base_fare / 2), 0]  # 피크에만 절반 부과 예시
        band_fare = {}
        for i, b in enumerate(band_cols):
            band_fare[b] = cols[i].number_input(
                b, 0, int(base_fare), min(int(defaults[i]), int(base_fare)), 50, key=f"bf_{i}")

        sub = bl[bl["노선"].isin(lines)] if lines else bl.iloc[0:0]
        rows = []
        for b in band_cols:
            rides = sub[b].sum() if not sub.empty else 0
            before = rides * base_fare          # 현행(무료) → 전액이 손실
            recovered = rides * band_fare[b]    # 부과분만큼 회복
            rows.append({"시간대": b, "현행 손실(전)": before / 1e8,
                         "개편후 잔여손실(후)": (before - recovered) / 1e8,
                         "회복액": recovered / 1e8})
        sim = pd.DataFrame(rows)
        tot_before = sim["현행 손실(전)"].sum()
        tot_recovered = sim["회복액"].sum()
        tot_after = sim["개편후 잔여손실(후)"].sum()
        pct = (tot_recovered / tot_before * 100) if tot_before else 0

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("현행 무임손실(전)", f"{tot_before:,.0f} 억원")
        m2.metric("회복액", f"{tot_recovered:,.1f} 억원")
        m3.metric("개편후 잔여손실(후)", f"{tot_after:,.0f} 억원")
        m4.metric("회복률", f"{pct:.1f}%")

        # 시간대별 전후 비교 막대그래프
        longdf = sim.melt(id_vars="시간대", value_vars=["현행 손실(전)", "개편후 잔여손실(후)"],
                          var_name="구분", value_name="억원")
        fig = px.bar(longdf, x="시간대", y="억원", color="구분", barmode="group", text="억원",
                     color_discrete_map={"현행 손실(전)": "#95A5A6", "개편후 잔여손실(후)": "#2E86C1"})
        fig.update_traces(texttemplate="%{text:.1f}", textposition="outside")
        fig.update_layout(height=430, legend_title="", margin=dict(t=30),
                          yaxis_title="무임손실(억원)", legend=dict(orientation="h", y=1.12))
        st.plotly_chart(fig, use_container_width=True)
        st.caption("회색=현행(전), 파랑=개편 후(후). 두 막대의 차이가 해당 시간대의 회복액입니다.")

        st.error(
            "**해석 주의 (중요).** 이 수치는 *행동 변화를 가정하지 않은 명목상 최대치*입니다. "
            "실제로는 ①요금이 생기면 이용을 줄이는 노인이 많아 그만큼 걷히지 않고, "
            "②이미 운행 중인 열차에 1명 더 태우는 한계비용은 0에 가까우며, "
            "③무임승차는 손실인 동시에 **노인 이동권·복지** 정책이기도 합니다. "
            "거리 추가운임도 제외했습니다. 따라서 *'이 조건에서 이론상 줄어드는 무임액의 상한'* 으로만 보세요."
        )
        with st.expander("시뮬레이션 상세표 / 호선×시간대 원자료"):
            st.dataframe(sim.round(1), hide_index=True, use_container_width=True)
            st.dataframe(bl, hide_index=True, use_container_width=True)

    # ── 🤖 AI 자문 ───────────────────────────────────────────────────────
    with tabs[3]:
        st.subheader("🤖 AI 정책 자문 (Gemini)")
        key = get_api_key()

        def build_ctx():
            ss = senior_by_station().head(8)
            bl = boardings_line_band()
            band_cols = list(BANDS.keys())
            bl2 = bl.copy(); bl2["전체"] = bl2[band_cols].sum(axis=1)
            tot = bl2["전체"].sum()
            p = ["[노인 이용 상위 역]"] + [f"- {r.역명}: {int(r.노인_연간이용):,}" for r in ss.itertuples()]
            p += ["", "[호선별 노인 승차(무임) 합계]"] + [
                f"- {r.노선}: {int(r.전체):,}" for r in bl2.sort_values('전체', ascending=False).itertuples()]
            p += ["", f"[전체 노인 승차 {int(tot):,}건, 기준요금 {BASE_FARE_DEFAULT}원 가정 시 "
                      f"명목 무임손실 약 {tot*BASE_FARE_DEFAULT/1e8:,.0f}억원 (행동변화 미반영)]"]
            return "\n".join(p)

        if not key:
            st.warning("왼쪽 사이드바에서 Gemini API 키를 입력하면 자문을 생성할 수 있습니다.")
        else:
            if st.button("📝 정책 자문 생성", type="primary"):
                with st.spinner("Gemini가 검토 중..."):
                    try:
                        st.session_state["advice"] = generate_advice(key, build_ctx())
                    except Exception as e:
                        st.error(f"오류: {e}")
            if st.session_state.get("advice"):
                st.markdown("---"); st.markdown(st.session_state["advice"])
        with st.expander("AI 입력 요약 보기"):
            st.code(build_ctx(), language="text")


if __name__ == "__main__":
    main()
