# -*- coding: utf-8 -*-
"""
scheduler.py — Railway 上 24h 常駐的排程器

工作:
  • 早上 08:00 (盤前): 掃處置股月線 → 存 GitHub → Telegram 推播
  • 晚上 21:00 (盤後): 掃全市場飆股 → 存 GitHub → Telegram 推播
  • 只在平日 (週一~週五) 執行

環境變數 (Railway Variables 設定):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   ← Telegram 推播
  GITHUB_TOKEN, GITHUB_REPO              ← 結果存回 GitHub
  MOMENTUM_LEVEL   (選填, 預設 all)       ← 晚上掃哪個策略
  MOMENTUM_PRESET  (選填, 預設 standard)  ← 用哪組參數
  PUSH_CHARTS      (選填, 預設 1)          ← 是否附 K 線圖
  MORNING_HHMM     (選填, 預設 08:00)
  EVENING_HHMM     (選填, 預設 21:00)
  RUN_ON_START     (選填, 預設 0)          ← 啟動就先跑一次 (測試用)
  TZ=Asia/Taipei                          ← 時區 (重要!)

本地測試:
  RUN_ON_START=1 python scheduler.py   ← 啟動立刻跑一次兩種掃描
"""
import os
import time
import traceback
from datetime import datetime

import schedule

from scan_tasks import run_momentum_scan, run_disposal_scan, save_result
from github_store import push_json
from notify_telegram import push_momentum, push_disposal, send_message


def _env(key, default=""):
    return os.environ.get(key, default)


def _log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}",
          flush=True)


def _is_weekday():
    return datetime.now().weekday() < 5


# ==================================================================
#   早上: 處置股
# ==================================================================
def job_morning_disposal(force=False):
    if not force and not _is_weekday():
        _log("週末, 跳過盤前處置股")
        return
    _log("▶ 開始盤前處置股掃描...")
    try:
        # 先讀 GitHub 上昨天的清單, 用來算「本日新增/出關」
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
        push_disposal(result, with_charts=with_charts)
        _log(f"✔ 盤前處置股完成, {result.get('scanned',0)} 檔")
    except Exception as e:
        _log(f"✘ 盤前處置股失敗: {e}\n{traceback.format_exc()}")
        send_message(f"❌ 盤前處置股排程失敗: {e}")


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
    morning = _env("MORNING_HHMM", "08:00")
    evening = _env("EVENING_HHMM", "21:00")

    _log("=" * 50)
    _log("台股排程器啟動")
    _log(f"  盤前處置股: 每個平日 {morning}")
    _log(f"  盤後飆股:   每個平日 {evening}")
    _log(f"  時區 TZ={_env('TZ', '(未設定, 建議設 Asia/Taipei)')}")
    _log(f"  現在時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S %A')}")
    _log("=" * 50)

    schedule.every().day.at(morning).do(job_morning_disposal)
    # ★ 飆股推播暫時關閉 (使用者要求先只留處置股)
    #   要恢復: 把下面這行取消註解
    # schedule.every().day.at(evening).do(job_evening_momentum)
    _log("  (飆股推播目前關閉, 只推處置股)")

    # 啟動即跑 (測試用)
    if _env("RUN_ON_START", "0") == "1":
        _log("RUN_ON_START=1 → 立即執行一次處置股掃描")
        job_morning_disposal(force=True)
        # job_evening_momentum(force=True)   # 飆股關閉

    _log("進入排程等待迴圈 (每 30 秒檢查一次)...")
    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            _log(f"排程迴圈錯誤: {e}")
        time.sleep(30)


if __name__ == "__main__":
    main()
