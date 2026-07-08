# -*- coding: utf-8 -*-
"""
streamlit_app.py v2 — 台股飆股篩選 Web App

v2 版面改動 (依使用者需求):
  1. ⚙️ 參數設定: 三種預設組一鍵切換 (保守/標準/寬鬆) + 進階微調
  2. 🎯 即時飆股篩選: 六種策略齊全 + 左表右圖 (點列即看 K 線)
  3. 🚨 處置股+月線: 左表右圖

執行:  streamlit run streamlit_app.py
"""
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

import os
import requests
from core_stock import (
    STRATEGY_LEVELS,
    StrategyParams, StockDataFetcher, MomentumScreener,
    FullMarketFetcher, scan_disposal_ma20,
)
from scan_tasks import run_momentum_scan, run_disposal_scan
from datetime import datetime


# ---- 讀 GitHub 上排程器算好的結果 (秒開) ----
# 在 Streamlit Secrets 或環境變數設 GITHUB_REPO = "yourname/tw-stock-app"
def _github_repo():
    try:
        return st.secrets.get("GITHUB_REPO", os.environ.get("GITHUB_REPO", ""))
    except Exception:
        return os.environ.get("GITHUB_REPO", "")


@st.cache_data(show_spinner=False, ttl=300)
def load_prebuilt(kind: str) -> dict | None:
    """從 GitHub raw 讀排程器預先算好的 JSON (momentum_all / disposal)"""
    repo = _github_repo()
    if not repo:
        return None
    branch = os.environ.get("GITHUB_BRANCH", "main")
    url = f"https://raw.githubusercontent.com/{repo}/{branch}/results/{kind}.json"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


# ---- 全市場資料載入 (快取: 同一天同參數只抓一次) ----
@st.cache_data(show_spinner=False, ttl=6 * 3600, max_entries=3)
def load_market_data(period: str, date_key: str, _pcb=None):
    # ★ v4: min_today_lots=0 完全不預過濾, 上市+上櫃全掃, 零漏網之魚
    stocks, data_date = FullMarketFetcher.fetch_stock_list(min_today_lots=0)
    if not stocks:
        return {}, {}, None
    names = {f"{s['code']}.{s.get('market', 'TW')}": s["name"]
             for s in stocks}
    frames = FullMarketFetcher.batch_download(stocks, period=period,
                                              progress_cb=_pcb)
    return frames, names, data_date

st.set_page_config(page_title="台股飆股篩選", page_icon="🎯",
                   layout="wide", initial_sidebar_state="collapsed")

# ---- CSS: 左右欄在手機上也不換行 (保持左右分割) ----
st.markdown("""
<style>
/* 平板/桌機/手機橫向 (>=768px): 強制左右並排不換行 */
@media (min-width: 768px) {
  [data-testid="stHorizontalBlock"] { flex-wrap: nowrap !important; gap: .6rem; }
  [data-testid="stHorizontalBlock"] > div { min-width: 0 !important; }
}
/* 手機直向 (<768px): 用預設堆疊, 表格在上 K 線在下, 各自全寬 */
div[data-testid="stDataFrame"] { font-size: 0.85rem; }
/* 縮小手機上的邊距 */
@media (max-width: 767px) {
  .block-container { padding-left: 0.6rem; padding-right: 0.6rem; }
}
</style>""", unsafe_allow_html=True)

st.title("🎯 台股飆股篩選")

# ================= session state 初始化 =================
if "preset" not in st.session_state:
    st.session_state.preset = "standard"
    StrategyParams.apply_preset("standard")
if "adv_params" not in st.session_state:
    st.session_state.adv_params = StrategyParams.get()

# 每次 rerun 都把 session 的參數灌回 StrategyParams (Streamlit 是多次執行模型)
StrategyParams.set_batch(st.session_state.adv_params)


# ================= Plotly K 線 =================
def plot_candlestick(ticker: str, name: str, note: str = "",
                     ma20_only: bool = False, disposal: dict = None):
    df = StockDataFetcher.fetch_history(ticker, period="6mo")
    if df.empty:
        st.error(f"無法取得 {ticker} 股價資料")
        return
    p = df.tail(120).copy()
    p["MA5"] = df["Close"].rolling(5).mean().loc[p.index]
    p["MA20"] = df["Close"].rolling(20).mean().loc[p.index]
    p["MA60"] = df["Close"].rolling(60).mean().loc[p.index]

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[0.75, 0.25], vertical_spacing=0.03)
    fig.add_trace(go.Candlestick(
        x=p.index, open=p["Open"], high=p["High"],
        low=p["Low"], close=p["Close"],
        increasing_line_color="#C62828", increasing_fillcolor="#C62828",
        decreasing_line_color="#2E7D32", decreasing_fillcolor="#2E7D32",
        name="K線"), row=1, col=1)
    fig.add_trace(go.Scatter(x=p.index, y=p["MA20"],
                             line=dict(color="#1E90FF", width=2),
                             name="20MA 月線"), row=1, col=1)
    if not ma20_only:
        fig.add_trace(go.Scatter(x=p.index, y=p["MA5"],
                                 line=dict(color="#FF8C00", width=1),
                                 name="5MA"), row=1, col=1)
        fig.add_trace(go.Scatter(x=p.index, y=p["MA60"],
                                 line=dict(color="#8B008B", width=1.2),
                                 name="60MA"), row=1, col=1)
    colors = ["#C62828" if c >= o else "#2E7D32"
              for c, o in zip(p["Close"], p["Open"])]
    fig.add_trace(go.Bar(x=p.index, y=p["Volume"] / 1000,
                         marker_color=colors, name="量(張)"), row=2, col=1)

    # 處置期間淡紅色帶
    if disposal and disposal.get("disposal_start") and disposal.get("disposal_end"):
        try:
            fig.add_vrect(x0=disposal["disposal_start"],
                          x1=disposal["disposal_end"],
                          fillcolor="#FF6B6B", opacity=0.15, line_width=0,
                          annotation_text="🚨 處置期間",
                          annotation_position="top left",
                          annotation_font_color="#C62828", row=1, col=1)
        except Exception:
            pass

    code = ticker.rsplit(".", 1)[0]
    fig.update_layout(
        title=f"{name} ({code})  {note}",
        xaxis_rangeslider_visible=False, height=560,
        margin=dict(l=8, r=8, t=48, b=8),
        legend=dict(orientation="h", y=1.02, x=0),
        hovermode="x unified",
        dragmode="pan")                     # 預設 = 平移 (不是框選縮放)
    fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])])
    # ★ 鎖定 Y 軸: 只能左右移動與 X 軸縮放, 不會上下滑飛走
    fig.update_yaxes(fixedrange=True)
    st.plotly_chart(
        fig, use_container_width=True,
        config={
            "scrollZoom": True,             # 滾輪 / 兩指縮放 (只作用 X 軸)
            "doubleClick": "reset",         # 雙擊 / 雙指點 = 還原視圖
            "displaylogo": False,
            "modeBarButtonsToRemove": [
                "select2d", "lasso2d", "zoom2d",
                "zoomIn2d", "zoomOut2d", "autoScale2d"],
        })
    st.caption("🖐️ 拖曳=左右平移  |  🤏 兩指/滾輪=縮放  |  👆👆 雙擊=還原")


# ================= 導航 (session_state 版, 修正 st.tabs 會跳回第一頁的問題) =================
PAGE_SCREENER = "🎯 即時飆股篩選"
PAGE_DISPOSAL = "🚨 處置股+月線"
page = st.radio("nav", [PAGE_SCREENER, PAGE_DISPOSAL],
                horizontal=True, key="nav_page",
                label_visibility="collapsed")
st.markdown("""<style>
div[role="radiogroup"] label { font-size: 1.05rem; padding: 2px 10px; }
</style>""", unsafe_allow_html=True)

# ============ Page 1: 即時飆股篩選 (參數 + 六策略 + 左表右圖) ============
if page == PAGE_SCREENER:
    # ---- 參數預設組 (原「參數設定」分頁併入此處) ----
    def _apply(preset):
        st.session_state.preset = preset
        st.session_state.adv_params = StrategyParams.apply_preset(preset)

    c1, c2, c3 = st.columns(3)
    with c1:
        st.button("🛡️ 保守", use_container_width=True,
                  help="訊號少, 品質高",
                  type="primary" if st.session_state.preset == "conservative" else "secondary",
                  on_click=_apply, args=("conservative",))
    with c2:
        st.button("⚖️ 標準", use_container_width=True,
                  help="平衡預設",
                  type="primary" if st.session_state.preset == "standard" else "secondary",
                  on_click=_apply, args=("standard",))
    with c3:
        st.button("🔥 寬鬆", use_container_width=True,
                  help="訊號多, 廣撒網",
                  type="primary" if st.session_state.preset == "aggressive" else "secondary",
                  on_click=_apply, args=("aggressive",))

    pv = st.session_state.adv_params
    with st.expander(f"🔧 參數微調  (目前: "
                     f"{StrategyParams.PRESET_LABELS[st.session_state.preset]})"):
        a1, a2 = st.columns(2)
        with a1:
            pv["vol_multiplier"] = st.slider("量能放大倍數", 1.0, 5.0,
                                             float(pv["vol_multiplier"]), 0.1)
            pv["min_vol_lots"] = st.number_input("最小20日均量(張)", 50, 10000,
                                                 int(pv["min_vol_lots"]), 50)
            pv["breakout_window"] = st.slider("突破回看天數", 10, 60,
                                              int(pv["breakout_window"]), 5)
            pv["max_extension_pct"] = st.slider("離50MA上限%", 5, 50,
                                                int(pv["max_extension_pct"]), 5)
        with a2:
            pv["rsi_min"], pv["rsi_max"] = st.slider(
                "RSI 區間", 0, 100,
                (int(pv["rsi_min"]), int(pv["rsi_max"])), 1)
            pv["require_macd_positive"] = st.checkbox(
                "MACD 柱狀體必須為正", value=bool(pv["require_macd_positive"]))
            pv["rs_min_count"] = st.slider("抗跌最少次數", 1, 10,
                                           int(pv["rs_min_count"]), 1)
            pv["rs_rating_min"] = st.slider("RS Rating 門檻", 0, 100,
                                            int(pv["rs_rating_min"]), 5)
        st.session_state.adv_params = pv
        StrategyParams.set_batch(pv)

    # ---- 策略選擇 ----
    level_labels = {v[0]: v[1] for v in STRATEGY_LEVELS}
    level = st.radio("進場條件策略 (六種)",
                     [v[0] for v in STRATEGY_LEVELS],
                     format_func=lambda x: level_labels[x],
                     index=0, key="scr_level")
    desc = {v[0]: v[2] for v in STRATEGY_LEVELS}[level]
    st.caption(f"💡 {desc}")

    # ---- 優先讀排程器算好的存檔 (秒開) ----
    prebuilt = load_prebuilt(f"momentum_{level}")
    if prebuilt and "screener_hits" not in st.session_state:
        st.session_state.screener_hits = prebuilt.get("items", [])
        st.session_state.market_info = (prebuilt.get("scanned", 0),
                                        prebuilt.get("data_date", ""))
        st.session_state.result_source = f"排程器 {prebuilt.get('generated_at','')}"

    c_scan, c_info = st.columns([1, 2])
    with c_scan:
        do_scan = st.button("🔄 立即重掃 (全市場)", type="primary",
                            use_container_width=True, key="scan_btn")
    with c_info:
        src = st.session_state.get("result_source")
        if src:
            st.caption(f"📋 目前顯示: {src} 的結果")
        st.caption("🌐 ~1800 檔上市+上櫃全掃, 首次即時掃描約 3~6 分鐘")
    if level == "strict":
        st.warning("⚠️ Strict 要抓 2 年資料, 首次會比較久 (~5 分鐘)")

    if do_scan:
        prog = st.progress(0, text="準備中...")
        period = "2y" if level == "strict" else "6mo"
        date_key = datetime.now().strftime("%Y-%m-%d")
        prog.progress(0.02, text="Stage 1/3: 抓上市+上櫃全部股票清單...")
        def _dl_cb(i, total, label):
            prog.progress(0.05 + 0.55 * i / max(total, 1),
                          text=f"Stage 2/3: {label}")
        frames, names, data_date = load_market_data(
            period, date_key, _pcb=_dl_cb)
        if not frames:
            st.error(f"抓取全市場清單失敗: "
                     f"{FullMarketFetcher.get_last_error()}")
            st.session_state.screener_hits = []
        else:
            def _scan_cb(i, total, label):
                prog.progress(0.60 + 0.40 * i / max(total, 1),
                              text=f"Stage 3/3: 篩選中 ({i}/{total}) {label}")
            st.session_state.screener_hits = MomentumScreener.scan_frames(
                frames, names, level=level, progress_cb=_scan_cb)
            st.session_state.market_info = (len(frames), data_date)
            st.session_state.result_source = (
                f"手動即時掃描 {datetime.now().strftime('%H:%M')}")
        prog.empty()

    hits = st.session_state.get("screener_hits", [])
    if hits:
        mi = st.session_state.get("market_info")
        extra = (f"  |  上市+上櫃共掃 {mi[0]} 檔 (資料日 {mi[1]})"
                 if mi else "")
        st.success(f"✅ 找到 {len(hits)} 檔{extra}")
        # ------- 左表右圖 -------
        col_l, col_r = st.columns([2, 3])
        with col_l:
            tbl = pd.DataFrame([{
                "代碼": h["ticker"].rsplit(".", 1)[0],
                "名稱": h["name"],
                "收盤": h["close"],
                "量比": h["vol_ratio"],
                "符合": h.get("matched", h["level"]),
            } for h in hits])
            event = st.dataframe(
                tbl, use_container_width=True, hide_index=True,
                on_select="rerun", selection_mode="single-row",
                height=420, key="scr_table")
        with col_r:
            rows = event.selection.rows if event and event.selection else []
            idx = rows[0] if rows else 0   # 沒點就先顯示第一名
            h = hits[idx]
            plot_candlestick(h["ticker"], h["name"],
                             note=f"量比 {h['vol_ratio']}x  "
                                  f"{h.get('matched','')}")
        st.caption("👈 點左表任一列, 右側 K 線立即切換")
    elif "screener_hits" in st.session_state:
        st.warning("❗ 沒有符合條件的個股 — 可切成 🔥 寬鬆預設組再試")


# ============ Page 2: 處置股+月線 (左表右圖) ============
else:
    c1, c2 = st.columns(2)
    with c1:
        days_back = st.selectbox("查詢區間", [7, 14, 30, 60], index=2,
                                 format_func=lambda x: f"最近 {x} 天")
    with c2:
        only_active = st.checkbox("只看處置仍生效中", value=True)

    # ---- 把攤平的存檔 item 轉回頁面用的巢狀結構 ----
    def _flat_to_nested(it):
        return {
            "code": it["code"], "name": it["name"],
            "close": it["close"], "ma20": it.get("ma20", 0),
            "diff_pct": it["diff_pct"], "abs_diff_pct": it["abs_diff_pct"],
            "color": it["color"], "change_5d_pct": it.get("change_5d_pct", 0),
            "disposal": {
                "disposal_start": it["disposal_start"],
                "disposal_end": it["disposal_end"],
                "measure": it.get("measure", ""),
                "is_active": it.get("is_active", False),
                "market": it.get("market", "TW"),
            },
        }

    # 優先讀排程器存檔
    prebuilt_d = load_prebuilt("disposal")
    if prebuilt_d and "disposal_results" not in st.session_state:
        st.session_state.disposal_results = [
            _flat_to_nested(it) for it in prebuilt_d.get("items", [])]
        st.session_state.disp_source = f"排程器 {prebuilt_d.get('generated_at','')}"

    c_btn, c_src = st.columns([1, 2])
    with c_btn:
        do_disp = st.button("🔄 立即重掃", type="primary",
                            use_container_width=True, key="disp_btn")
    with c_src:
        ds = st.session_state.get("disp_source")
        if ds:
            st.caption(f"📋 目前顯示: {ds} 的結果")

    if do_disp:
        prog = st.progress(0, text="抓取處置股清單...")
        def _cb2(i, total, label):
            prog.progress(i / max(total, 1),
                          text=f"計算月線 ({i}/{total}) {label}")
        st.session_state.disposal_results = scan_disposal_ma20(
            days_back=days_back, only_active=only_active, progress_cb=_cb2)
        st.session_state.disp_source = (
            f"手動即時掃描 {datetime.now().strftime('%H:%M')}")
        prog.empty()

    results = st.session_state.get("disposal_results", [])
    if results:
        st.success(f"✅ 共 {len(results)} 檔  (🔴 ≤2%  🟡 ≤5%  ⚪ >5%)")
        col_l, col_r = st.columns([2, 3])
        with col_l:
            def _short(dstr):  # 2026-06-22 → 06/22
                return dstr[5:].replace("-", "/") if dstr else "?"
            tbl = pd.DataFrame([{
                "": r["color"], "代碼": r["code"], "名稱": r["name"],
                "市場": "櫃" if r["disposal"].get("market") == "TWO" else "市",
                "現價": r["close"],
                "距月線%": r["diff_pct"],
                "處置期間": f"{_short(r['disposal']['disposal_start'])}"
                            f"~{_short(r['disposal']['disposal_end'])}",
                "狀態": "處置中" if r["disposal"]["is_active"] else "已結束",
                "5日%": r["change_5d_pct"],
                "措施": r["disposal"]["measure"],
            } for r in results])
            event = st.dataframe(
                tbl, use_container_width=True, hide_index=True,
                on_select="rerun", selection_mode="single-row",
                height=420, key="disp_table")
        with col_r:
            rows = event.selection.rows if event and event.selection else []
            idx = rows[0] if rows else 0
            r = results[idx]
            d = r["disposal"]
            plot_candlestick(
                f"{r['code']}.{r['disposal'].get('market', 'TW')}", r["name"],
                note=f"距月線 {r['diff_pct']:+.1f}%",
                ma20_only=True, disposal=d)
            st.caption(f"🚨 處置 {d['disposal_start']} ~ {d['disposal_end']}  "
                       f"({d['measure']})  "
                       f"{'✅ 生效中' if d['is_active'] else '已結束'}")
        st.caption("👈 點左表任一列, 右側 K 線立即切換")
    elif "disposal_results" in st.session_state:
        from core_stock import DisposalStockFetcher
        err = DisposalStockFetcher.get_last_error()
        if err:
            st.error(f"❌ 抓取失敗: {err}\n\n"
                     f"💡 若你在雲端 (streamlit.app) 看到此訊息, "
                     f"通常是證交所擋海外 IP — 已內建 OpenAPI 備援會自動重試, "
                     f"若兩者都失敗請把此訊息回報。")
        else:
            st.warning("❗ 目前沒有符合條件的處置股 "
                       "(可取消「只看處置仍生效中」或拉長查詢區間)")

st.divider()
st.caption("📊 資料來源: Yahoo Finance / 證交所  |  僅供研究參考, 不構成投資建議")
