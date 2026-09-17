#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自动更新K线数据脚本（v2 · 2026-09-17）
每个交易日收盘后运行，更新仓库根目录的 kline.json（选股器外置K线底座）。

数据源顺序（全部免费、无需密钥）：
  1. 腾讯财经 web.ifzq.gtimg.cn —— 前复权日K，云服务器IP稳定（主源）
  2. 新浪财经 money.finance.sina.com.cn —— 不复权日K（备源，兜底）
  3. 东方财富 push2his.eastmoney.com —— 前复权日K（末位兜底，云IP常被限流）

输出格式与前端 getRealKline() 完全对齐：
  kline.json = {"000001": "最高,最低,收盘;最高,最低,收盘;...", ...}
  价格为整数（元×100，四舍五入），每只保留最近 125 根日K。

用法：
  python update_kline.py                # 正常更新（收盘后跑，15:00前自动丢弃当日未收盘K线）
  python update_kline.py --limit 20     # 只更新前20只（冒烟测试）
  python update_kline.py --workers 12   # 调整并发
  python update_kline.py --include-intraday  # 允许写入当日盘中K线（默认不允许）
"""

import json
import shutil
import argparse
import time
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests

# ===== 配置 =====
KLINE_BARS = 125          # 每只股票保留的日K根数（与前端底座一致）
WORKERS = 8               # 默认并发
RETRY = 3                 # 每个数据源失败重试次数（腾讯网关偶发501，重试可恢复）
TIMEOUT = 10
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

SCRIPT_DIR = Path(__file__).resolve().parent


def locate_kline_file():
    """kline.json 优先在脚本同目录（仓库根部署），其次在父目录（本仓库开发结构）"""
    for cand in (SCRIPT_DIR / "kline.json", SCRIPT_DIR.parent / "kline.json"):
        if cand.exists():
            return cand
    # 默认输出到脚本同目录
    return SCRIPT_DIR / "kline.json"


def tx_symbol(code):
    return ("sh" if code.startswith("6") else "sz") + code


def sina_symbol(code):
    return ("sh" if code.startswith("6") else "sz") + code


def em_secid(code):
    return ("1." if code.startswith("6") else "0.") + code


def http_get_json(url, referer=None, encoding=None):
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    resp = requests.get(url, headers=headers, timeout=TIMEOUT)
    resp.raise_for_status()
    if encoding:
        resp.encoding = encoding
    return resp.json()


# ===== 数据源：统一返回 [{date, open, close, high, low}, ...]（升序）=====

TENCENT_GATEWAYS = [
    # 主网关；偶发WAF 501时由备用网关接管（同一套腾讯行情数据）
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
]


def fetch_tencent(code):
    """腾讯前复权日K（主源，与kline.json底座复权方式一致），双网关自动切换"""
    sym = tx_symbol(code)
    last_err = None
    for gw in TENCENT_GATEWAYS:
        try:
            url = f"{gw}?param={sym},day,,,{KLINE_BARS + 5},qfq"
            data = http_get_json(url, referer="https://gu.qq.com/")
            node = data["data"][sym]
            rows = node.get("qfqday") or node.get("day")
            if rows:
                return [{
                    "date": r[0],
                    "open": float(r[1]),
                    "close": float(r[2]),
                    "high": float(r[3]),
                    "low": float(r[4]),
                } for r in rows]
        except Exception as e:
            last_err = e
            continue  # 该网关失败（如WAF 501），换备用网关
    if last_err:
        raise last_err
    return None


def fetch_sina(code):
    """新浪不复权日K（备源）"""
    sym = sina_symbol(code)
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={KLINE_BARS + 5}")
    arr = http_get_json(url, referer="https://finance.sina.com.cn/")
    if not arr:
        return None
    return [{
        "date": r["day"],
        "open": float(r["open"]),
        "close": float(r["close"]),
        "high": float(r["high"]),
        "low": float(r["low"]),
    } for r in arr]


def fetch_eastmoney(code):
    """东方财富前复权日K（末位兜底，云服务器IP常被限流/封禁）"""
    import random
    host = random.choice(["push2his.eastmoney.com", "82.push2his.eastmoney.com",
                          "19.push2his.eastmoney.com", "push2delay.eastmoney.com"])
    url = (f"https://{host}/api/qt/stock/kline/get?secid={em_secid(code)}"
           "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
           f"&klt=101&fqt=1&end=20500101&lmt={KLINE_BARS + 5}"
           "&ut=bd1d9ddb04089700cf9c27f6f742628")
    data = http_get_json(url, referer="https://quote.eastmoney.com/")
    rows = (data.get("data") or {}).get("klines")
    if not rows:
        return None
    out = []
    for line in rows:
        p = line.split(",")
        out.append({
            "date": p[0],
            "open": float(p[1]),
            "close": float(p[2]),
            "high": float(p[3]),
            "low": float(p[4]),
        })
    return out


SOURCES = [("tencent", fetch_tencent), ("sina", fetch_sina), ("eastmoney", fetch_eastmoney)]


def fetch_with_fallback(code):
    """依次尝试三个数据源，每个重试 RETRY 次。返回 (klines, 来源名) 或 (None, None)"""
    for name, fn in SOURCES:
        for attempt in range(RETRY + 1):
            try:
                klines = fn(code)
                if klines and len(klines) >= 20:
                    return klines, name
                break  # 该源返回空数据（非异常），直接换下一个源
            except Exception:
                if attempt < RETRY:
                    time.sleep(0.5 * (2 ** attempt))  # 0.5s / 1s / 2s 指数退避
                    continue
            break  # 重试耗尽，换下一个源
    return None, None


def fetch_tencent_only(code, retries=5):
    """只拉腾讯前复权源（用于把新浪不复权兜底股补刷回前复权）"""
    for attempt in range(retries):
        try:
            klines = fetch_tencent(code)
            if klines and len(klines) >= 20:
                return klines
        except Exception:
            pass
        if attempt < retries - 1:
            time.sleep(1.0 * (attempt + 1))  # 1s/2s/3s/4s，等腾讯网关501窗口过去
    return None


def encode_compact(klines):
    """编码为前端格式：最高,最低,收盘;...（价格×100取整）"""
    return ";".join(
        f"{round(k['high'] * 100)},{round(k['low'] * 100)},{round(k['close'] * 100)}"
        for k in klines
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="只更新前N只（测试）")
    parser.add_argument("--workers", type=int, default=WORKERS)
    parser.add_argument("--include-intraday", action="store_true",
                        help="允许写入当日未收盘K线（默认收盘前运行时丢弃当日）")
    args = parser.parse_args()

    kline_path = locate_kline_file()
    print("=" * 60)
    print(f"开始更新K线: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"数据文件: {kline_path}")

    # 1. 读现有底座（股票池以底座为准，不依赖任何"全市场列表"接口）
    with open(kline_path, "r", encoding="utf-8") as f:
        old_data = json.load(f)
    codes = list(old_data.keys())
    if args.limit:
        codes = codes[:args.limit]
    print(f"待更新股票: {len(codes)} 只，并发 {args.workers}")

    # 收盘前（本地时间15:00前）默认不写入当日盘中K线，避免底座出现未收盘快照
    now = datetime.now()
    is_weekday = now.weekday() < 5
    cutoff_passed = (now.hour, now.minute) >= (15, 0)
    drop_today = None
    if not args.include_intraday and not (is_weekday and cutoff_passed):
        drop_today = now.strftime("%Y-%m-%d")
        print(f"提示: 非收盘后时段，丢弃当日({drop_today})未收盘K线")

    # 2. 并发拉取
    new_data = dict(old_data)  # 失败的股票保留旧数据
    stats = {"tencent": 0, "sina": 0, "eastmoney": 0}
    source_map = {}  # code -> 数据源名称
    failed = []
    done = 0

    def normalize(klines):
        klines.sort(key=lambda k: k["date"])
        if drop_today and klines and klines[-1]["date"] == drop_today:
            klines = klines[:-1]
        klines = klines[-KLINE_BARS:]
        return klines if len(klines) >= 20 else None

    def work(code):
        klines, src = fetch_with_fallback(code)
        if not klines:
            return code, None, None
        klines = normalize(klines)
        if not klines:
            return code, None, None
        return code, encode_compact(klines), src

    # 第一遍：全量，腾讯→新浪→东财三级兜底
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(work, c) for c in codes]
        for fut in as_completed(futures):
            code, encoded, src = fut.result()
            done += 1
            if encoded:
                new_data[code] = encoded
                stats[src] += 1
                source_map[code] = src
            else:
                failed.append(code)
            if done % 200 == 0 or done == len(codes):
                print(f"进度 {done}/{len(codes)}  成功{done - len(failed)}  失败{len(failed)}")

    # 第二阶段：新浪（不复权）兜底股低并发回补腾讯前复权源
    # 腾讯网关501是阵发性的，等待后低并发重试通常可恢复，保证底座复权方式统一
    for round_idx, (wait_sec, rworkers) in enumerate([(20, 3), (40, 2)], start=1):
        need = [c for c in codes if source_map.get(c) == "sina"]
        if not need:
            break
        print(f"第{round_idx}轮腾讯前复权补刷: {len(need)}只新浪兜底股，等待{wait_sec}秒后低并发重试...")
        time.sleep(wait_sec)
        recovered = 0
        with ThreadPoolExecutor(max_workers=rworkers) as pool:
            fut_map = {pool.submit(fetch_tencent_only, c): c for c in need}
            for fut in as_completed(fut_map):
                code = fut_map[fut]
                try:
                    klines = normalize(fut.result())
                except Exception:
                    klines = None
                if klines:
                    new_data[code] = encode_compact(klines)
                    stats["sina"] -= 1
                    stats["tencent"] += 1
                    source_map[code] = "tencent"
                    recovered += 1
        print(f"第{round_idx}轮补刷恢复 {recovered}/{len(need)} 只")

    # 3. 写出（先备份）
    backup = kline_path.with_suffix(".json.bak")
    shutil.copy2(kline_path, backup)
    with open(kline_path, "w", encoding="utf-8") as f:
        json.dump(new_data, f, ensure_ascii=False, separators=(",", ":"))

    print("-" * 60)
    print(f"完成: 腾讯 {stats['tencent']} / 新浪 {stats['sina']} / 东财 {stats['eastmoney']}")
    print(f"失败 {len(failed)} 只（已保留旧数据）: {failed[:20]}{' ...' if len(failed) > 20 else ''}")
    print(f"备份: {backup}")

    if failed:
        # 抽查一只成功股票的最后日期
        sample = next((c for c in codes if c not in failed), None)
        if sample:
            print(f"抽查 {sample} 末尾: {new_data[sample].split(';')[-1]}（压缩格式，无日期）")

    # 失败率超过10%判定异常（CI可见），但底座已写出（成功部分仍生效）
    fail_rate = len(failed) / max(len(codes), 1)
    if fail_rate > 0.10:
        print(f"错误: 失败率 {fail_rate:.1%}，超过10%，请检查数据源")
        sys.exit(1)
    print("更新成功。")


if __name__ == "__main__":
    main()
