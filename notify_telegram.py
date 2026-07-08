# -*- coding: utf-8 -*-
"""
notify_telegram.py — Telegram 推播 (文字訊息 + K 線 PNG)

不依賴 python-telegram-bot 的 Application (那是給互動 bot 的),
排程推播只要單純打 Telegram Bot HTTP API 即可, 更輕量。

需要環境變數:
  TELEGRAM_BOT_TOKEN — 找 @BotFather /newbot 拿到
  TELEGRAM_CHAT_ID   — 你的 chat id (找 @userinfobot 拿, 或群組 id)
"""
import os
import io
import time
import requests

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import mplfinance as mpf

from core_stock import StockDataFetcher


def _cfg():
    return (os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            os.environ.get("TELEGRAM_CHAT_ID", ""))


def _setup_font():
    from matplotlib import font_manager
    for c in ["Microsoft JhengHei", "PingFang TC", "Noto Sans CJK TC",
              "WenQuanYi Micro Hei", "Noto Sans TC", "SimHei"]:
        if c in {f.name for f in font_manager.fontManager.ttflist}:
            plt.rcParams["font.family"] = c
            break
    plt.rcParams["axes.unicode_minus"] = False


_setup_font()


def send_message(text: str, parse_mode: str = None) -> bool:
    token, chat_id = _cfg()
    if not token or not chat_id:
        print("[notify] 缺 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text,
               "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        r = requests.post(url, json=payload, timeout=20)
        return r.status_code == 200
    except Exception as e:
        print(f"[notify] send_message 失敗: {e}")
        return False


def send_photo(png_bytes: bytes, caption: str = "") -> bool:
    token, chat_id = _cfg()
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    try:
        files = {"photo": ("chart.png", png_bytes, "image/png")}
        data = {"chat_id": chat_id, "caption": caption[:1000]}
        r = requests.post(url, data=data, files=files, timeout=30)
        return r.status_code == 200
    except Exception as e:
        print(f"[notify] send_photo 失敗: {e}")
        return False


def make_kline_png(ticker: str, name: str, note: str = "",
                   ma20_only: bool = False) -> bytes | None:
    """產生 K 線 PNG (台股紅漲綠跌 + 均線 + 量能)"""
    df = StockDataFetcher.fetch_history(ticker, period="6mo")
    if df.empty:
        return None
    plot_df = df.tail(120).copy()
    mc = mpf.make_marketcolors(up="#C62828", down="#2E7D32",
                               edge="inherit", wick="inherit",
                               volume="inherit")
    style = mpf.make_mpf_style(marketcolors=mc, gridstyle=":",
                               y_on_right=True, rc={"font.size": 9})
    mav = (20,) if ma20_only else (5, 20, 60)
    code = ticker.rsplit(".", 1)[0]
    buf = io.BytesIO()
    try:
        mpf.plot(plot_df, type="candle", volume=True, mav=mav, style=style,
                 title=f"\n{code} {name}  {note}",
                 figsize=(10, 7), tight_layout=True,
                 savefig=dict(fname=buf, dpi=110, format="png"))
        buf.seek(0)
        return buf.getvalue()
    except Exception as e:
        print(f"[notify] 繪圖失敗 {ticker}: {e}")
        return None
    finally:
        plt.close("all")


# ==================================================================
#   組裝推播: 飆股 / 處置股
# ==================================================================
def push_momentum(result: dict, top_n: int = 10, with_charts: bool = True):
    """晚上盤後推飆股"""
    items = result.get("items", [])
    level = result.get("level", "")
    lv_name = {"basic": "Basic", "standard": "Standard", "strict": "Strict",
               "channel": "Channel", "rs_strong": "RS抗跌",
               "all": "All(綜合)"}.get(level, level)

    if result.get("error"):
        send_message(f"⚠️ 飆股掃描發生問題: {result['error']}")
    if not items:
        send_message(f"🌙 盤後飆股 [{lv_name}]\n"
                     f"今日無符合條件個股 (掃 {result.get('scanned',0)} 檔)")
        return

    header = (f"🌙 盤後飆股結算 [{lv_name}]\n"
             f"資料日 {result.get('data_date','')}  "
             f"掃描 {result.get('scanned',0)} 檔\n"
             f"符合 {len(items)} 檔, 前 {min(top_n,len(items))} 名:\n"
             f"{'━'*20}")
    lines = [header]
    for i, h in enumerate(items[:top_n], 1):
        code = h["ticker"].rsplit(".", 1)[0]
        mk = "櫃" if h["ticker"].endswith(".TWO") else "市"
        lines.append(
            f"{i}. {code}({mk}) {h['name']}  "
            f"收{h['close']} 量比{h['vol_ratio']}x\n"
            f"    {h.get('matched','')}")
    send_message("\n".join(lines))

    if with_charts:
        for h in items[:top_n]:
            code = h["ticker"].rsplit(".", 1)[0]
            png = make_kline_png(h["ticker"], h["name"],
                                 note=f"量比{h['vol_ratio']}x {h.get('matched','')}")
            if png:
                send_photo(png, caption=f"📈 {code} {h['name']}")
                time.sleep(0.5)  # 避免觸發 Telegram 限流


def push_disposal(result: dict, top_n: int = 15, with_charts: bool = True):
    """早上盤前推處置股"""
    items = result.get("items", [])
    if result.get("error"):
        send_message(f"⚠️ 處置股掃描提醒: {result['error']}")
    if not items:
        send_message("🌅 盤前處置股提醒\n目前無處置生效中的普通股")
        return

    header = (f"🌅 盤前處置股提醒 ({result.get('data_date','')})\n"
             f"處置生效中 {len(items)} 檔 (依距月線排序)\n"
             f"🔴≤2% 🟡≤5% ⚪>5%\n{'━'*20}")
    lines = [header]
    for r in items[:top_n]:
        mk = "櫃" if r.get("market") == "TWO" else "市"
        lines.append(
            f"{r['color']} {r['code']}({mk}) {r['name']}  "
            f"距月線{r['diff_pct']:+.1f}%\n"
            f"    處置 {r['disposal_start']}~{r['disposal_end']}")
    send_message("\n".join(lines))

    if with_charts:
        # 只附「接近月線」(🔴🟡) 的圖, 避免圖太多
        near = [r for r in items if r["abs_diff_pct"] <= 5][:top_n]
        for r in near:
            ticker = f"{r['code']}.{r.get('market','TW')}"
            png = make_kline_png(ticker, r["name"],
                                 note=f"距月線{r['diff_pct']:+.1f}%",
                                 ma20_only=True)
            if png:
                send_photo(png, caption=f"📈 {r['code']} {r['name']} "
                                        f"(處置中, 距月線{r['diff_pct']:+.1f}%)")
                time.sleep(0.5)
