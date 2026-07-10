# -*- coding: utf-8 -*-
"""
scheduler.py — Railway 上 24h 常駐的排程器

工作:
  • 早上 08:00 (盤前): 掃處置股月線 → 存 GitHub → Telegram 推播
  • 晚上 21:00 (盤後): 掃全市場飆股 → 存 GitHub → Telegram 推播
  • 只在平日 (週一~週五) 執行

環境變數 (Railway Variables 設定):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   ← Telegram 推播 (選填)
  LINE_CHANNEL_ACCESS_TOKEN, LINE_TO     ← LINE 推播 (選填, 見 DEPLOY_LINE.md)
  GITHUB_TOKEN, GITHUB_REPO              ← 結果+K線圖存回 GitHub (必填)
  PUSH_CHARTS      (選填, 預設 1)          ← 是否附 K 線圖
  MORNING_HHMM     (選填, 預設 08:00)      ← 早上提醒時段
  EVENING_HHMM     (選填, 預設 18:00)      ← 傍晚收盤後最新 (證交所公告已出)
  RUN_ON_START     (選填, 預設 0)          ← 啟動就先跑一次 (測試用)
  TZ=Asia/Taipei                          ← 時區 (重要!)

處置股推播時段 (兩者都完整版含K線):
  • 傍晚 18:00 收盤後: 證交所公告已出, 抓當日最新處置名單
  • 早上 08:00 提醒:   開盤前再提醒今天哪些股票在處置中

本地測試:
  RUN_ON_START=1 python scheduler.py
"""
import os
import time
import traceback
from datetime import datetime

import schedule

from scan_tasks import run_momentum_scan, run_disposal_scan, save_result
from github_store import push_json, push_bytes
from core_stock import is_market_open_today, is_fixed_holiday
from notify_telegram import (push_momentum, push_disposal, send_message,
                             make_kline_png)
import notify_line


def _env(key, default=""):
    return os.environ.get(key, default)


def _log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}",
          flush=True)


def _is_weekday():
    return datetime.now().weekday() < 5


# ==================================================================
#   處置股推播 (含開盤判斷 + 重試機制)
# ==================================================================
# 記錄「哪一天已成功推播過」, 避免重試時重複推
_last_pushed_date = {"disposal": None}


def _notify_all(text: str):
    """同時發 Telegram + LINE 純文字通知"""
    if _env("TELEGRAM_BOT_TOKEN") and _env("TELEGRAM_CHAT_ID"):
        try:
            send_message(text)
        except Exception:
            pass
    if _env("LINE_CHANNEL_ACCESS_TOKEN") and _env("LINE_TO"):
        try:
            notify_line.send_text(text)
        except Exception:
            pass


def try_disposal_push(slot_label="", is_last_attempt=False):
    """
    嘗試推播處置股, 但只在「證交所今天資料已就緒」時才推。

    slot_label: 這次是哪個時段觸發的 (18:00/20:00...) 用於 log
    is_last_attempt: 是否為最後一次嘗試 (00:00), 若仍失敗會發「未抓到」通知
    """
    today_str = datetime.now().strftime("%Y-%m-%d")

    # 今天已經成功推過 → 跳過 (避免重試時重複)
    if _last_pushed_date["disposal"] == today_str:
        _log(f"[{slot_label}] 今日已推播過, 跳過")
        return

    # 固定國定假日 → 完全不推 (連檢查都省)
    if is_fixed_holiday():
        _log(f"[{slot_label}] 今日為國定假日, 不推播")
        _last_pushed_date["disposal"] = today_str  # 標記今天不用再試
        return

    # 檢查證交所今天有沒有開盤/更新
    ready, reason = is_market_open_today()
    _log(f"[{slot_label}] 開盤檢查: {reason}")

    if not ready:
        if is_last_attempt:
            # 最後一次仍沒資料 → 判斷是假日還是異常
            # 週末/明確假日不發, 其他 (可能颱風假或證交所異常) 發通知
            wd = datetime.now().weekday()
            if wd >= 5:
                _log(f"[{slot_label}] 週末, 不發通知")
            else:
                _notify_all(f"📭 台股處置股提醒 ({today_str})\n"
                            f"今日到 00:00 仍未抓到證交所行情資料。\n"
                            f"可能原因: 颱風假/臨時休市/證交所延遲。\n"
                            f"(系統運作正常, 僅告知)")
                _log(f"[{slot_label}] 已發送「未抓到資料」通知")
            _last_pushed_date["disposal"] = today_str
        else:
            _log(f"[{slot_label}] 今日資料未就緒, 等待下一時段重試")
        return

    # 資料就緒 → 正式推播
    _log(f"[{slot_label}] ✅ 資料就緒, 開始推播")
    job_disposal(force=True, label=f"處置股({slot_label})")
    _last_pushed_date["disposal"] = today_str


def _gen_and_upload_charts(result: dict) -> dict:
    """對接近月線(🔴🟡)的處置股產生 K 線圖並 push 到 GitHub, 回傳 {code: raw_url}"""
    chart_urls = {}
    near = [r for r in result.get("items", []) if r["abs_diff_pct"] <= 5]
    for r in near:
        ticker = f"{r['code']}.{r.get('market', 'TW')}"
        png = make_kline_png(ticker, r["name"],
                             note=f"距月線{r['diff_pct']:+.1f}%",
                             ma20_only=True)
        if not png:
            continue
        path = f"results/charts/disposal_{r['code']}.png"
        ok, url = push_bytes(path, png,
                             commit_msg=f"chart {r['code']}")
        if ok:
            chart_urls[r["code"]] = url
    return chart_urls


def job_disposal(force=False, label="處置股"):
    if not force and not _is_weekday():
        _log(f"週末, 跳過{label}")
        return
    _log(f"▶ 開始{label}掃描...")
    try:
        prev_codes = _load_prev_disposal_codes()
        result = run_disposal_scan(days_back=30, only_active=True,
                                   prev_codes=prev_codes)
        save_result("disposal", result)
        ok, msg = push_json("results/disposal.json", result,
                            commit_msg=f"disposal {result.get('data_date')}")
        _log(f"  GitHub 存檔: {'OK' if ok else msg}")
        _log(f"  本日新增 {len(result.get('added_today',[]))} 檔, "
             f"出關 {len(result.get('removed_today',[]))} 檔")

        with_charts = _env("PUSH_CHARTS", "1") == "1"

        chart_urls = {}
        if with_charts:
            chart_urls = _gen_and_upload_charts(result)
            _log(f"  已產生 {len(chart_urls)} 張 K 線圖")

        if _env("TELEGRAM_BOT_TOKEN") and _env("TELEGRAM_CHAT_ID"):
            try:
                push_disposal(result, with_charts=with_charts)
                _log("  ✔ Telegram 推播完成")
            except Exception as e:
                _log(f"  ✘ Telegram 推播失敗: {e}")

        if _env("LINE_CHANNEL_ACCESS_TOKEN") and _env("LINE_TO"):
            try:
                notify_line.push_disposal_line(result, chart_urls=chart_urls)
                _log("  ✔ LINE 推播完成")
            except Exception as e:
                _log(f"  ✘ LINE 推播失敗: {e}")

        _log(f"✔ {label}完成, {result.get('scanned',0)} 檔")
    except Exception as e:
        _log(f"✘ {label}失敗: {e}\n{traceback.format_exc()}")
        _notify_all(f"❌ {label}排程失敗: {e}")


# 早上時段 (盤前提醒): 直接推, 不用等當日資料 (推的是昨天收盤後已定的名單)
def job_morning_disposal(force=False):
    if not force and not _is_weekday():
        _log("週末, 跳過盤前提醒")
        return
    if is_fixed_holiday():
        _log("國定假日, 跳過盤前提醒")
        return
    job_disposal(force=force, label="盤前提醒")


def _load_prev_disposal_codes():
    """讀 GitHub 上現有的 disposal.json, 取出昨天的處置代碼集合"""
    import requests as _rq
    repo = _env("GITHUB_REPO", "")
    branch = _env("GITHUB_BRANCH", "main")
    if not repo:
        return None
    url = f"https://raw.githubusercontent.com/{repo}/{branch}/results/disposal.json"
    try:
        r = _rq.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            # 用 all_codes 或從 items 取
            if "all_codes" in data:
                return set(data["all_codes"])
            return {it["code"] for it in data.get("items", [])}
    except Exception as e:
        _log(f"  (讀取昨日清單失敗, 首次執行屬正常: {e})")
    return None


# ==================================================================
#   晚上: 飆股
# ==================================================================
def job_evening_momentum(force=False):
    if not force and not _is_weekday():
        _log("週末, 跳過盤後飆股")
        return
    level = _env("MOMENTUM_LEVEL", "all")
    preset = _env("MOMENTUM_PRESET", "standard")
    _log(f"▶ 開始盤後飆股掃描 (level={level}, preset={preset})...")
    try:
        def _pcb(ratio, text):
            if int(ratio * 100) % 20 == 0:
                _log(f"  {int(ratio*100)}% {text}")
        result = run_momentum_scan(level=level, preset=preset,
                                   progress_cb=_pcb)
        save_result(f"momentum_{level}", result)
        ok, msg = push_json(f"results/momentum_{level}.json", result,
                            commit_msg=f"momentum {result.get('data_date')}")
        _log(f"  GitHub 存檔: {'OK' if ok else msg}")
        with_charts = _env("PUSH_CHARTS", "1") == "1"
        push_momentum(result, with_charts=with_charts)
        _log(f"✔ 盤後飆股完成, 掃 {result.get('scanned',0)} 檔, "
             f"符合 {len(result.get('items',[]))} 檔")
    except Exception as e:
        _log(f"✘ 盤後飆股失敗: {e}\n{traceback.format_exc()}")
        send_message(f"❌ 盤後飆股排程失敗: {e}")


def main():
    morning = _env("MORNING_HHMM", "08:00")     # 早上提醒

    tg_on = bool(_env("TELEGRAM_BOT_TOKEN") and _env("TELEGRAM_CHAT_ID"))
    line_on = bool(_env("LINE_CHANNEL_ACCESS_TOKEN") and _env("LINE_TO"))

    _log("=" * 50)
    _log("台股處置股排程器啟動")
    _log(f"  🌆 收盤後推最新 (含重試): 18:00 → 20:00 → 22:00 → 00:00")
    _log(f"     每時段先檢查證交所今日資料是否就緒, 就緒才推, 只推一次")
    _log(f"     到 00:00 仍無資料 → 發「未抓到」通知 (假日/颱風除外)")
    _log(f"  🌅 早上提醒: 每個平日 {morning}")
    _log(f"  推播管道: Telegram={'ON' if tg_on else 'off'}  "
         f"LINE={'ON' if line_on else 'off'}")
    _log(f"  時區 TZ={_env('TZ', '(未設定, 建議設 Asia/Taipei)')}")
    _log(f"  現在時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S %A')}")
    _log("=" * 50)

    # 傍晚重試序列: 18→20→22→00, 每時段檢查資料就緒才推 (已推過會自動跳過)
    schedule.every().day.at("18:00").do(
        lambda: try_disposal_push("18:00"))
    schedule.every().day.at("20:00").do(
        lambda: try_disposal_push("20:00"))
    schedule.every().day.at("22:00").do(
        lambda: try_disposal_push("22:00"))
    schedule.every().day.at("00:00").do(
        lambda: try_disposal_push("00:00", is_last_attempt=True))

    # 早上提醒 (推的是前一交易日收盤後已定的名單, 不需等當日資料)
    schedule.every().day.at(morning).do(job_morning_disposal)

    # 飆股推播目前關閉 (要恢復把下行取消註解)
    # schedule.every().day.at("21:00").do(job_evening_momentum)

    # 啟動即跑 (測試用) — 強制推一次, 略過開盤檢查
    if _env("RUN_ON_START", "0") == "1":
        _log("RUN_ON_START=1 → 立即強制執行一次 (略過開盤檢查)")
        job_disposal(force=True, label="測試處置股")

    _log("進入排程等待迴圈 (每 30 秒檢查一次)...")
    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            _log(f"排程迴圈錯誤: {e}")
        time.sleep(30)


if __name__ == "__main__":
    main()
