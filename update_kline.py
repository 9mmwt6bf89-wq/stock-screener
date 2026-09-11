#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import re, json, requests
from datetime import datetime

INDEX_FILE = "index.html"
EASTMONEY_KLINE_URL = "http://push2his.eastmoney.com/api/qt/stock/kline/get"
EASTMONEY_LIST_URL = "http://80.push2.eastmoney.com/api/qt/clist/get"

def get_all_stock_codes():
    params = {"pn":1,"pz":5000,"po":1,"np":1,"fltt":2,"invt":2,"fid":12,
              "fs":"m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048",
              "fields":"f12"}
    try:
        resp = requests.get(EASTMONEY_LIST_URL, params=params, timeout=10)
        data = resp.json()
        return [item["f12"] for item in data["data"]["diff"]]
    except Exception as e:
        print(f"获取股票列表失败: {e}")
        return []

def get_today_kline(code):
    secid = f"1.{code}" if code.startswith("6") else f"0.{code}"
    today = datetime.now().strftime("%Y-%m-%d")
    params = {"secid":secid,"fields1":"f1,f2,f3,f4,f5,f6",
              "fields2":"f51,f52,f53,f54,f55,f56,f57,f58",
              "klt":101,"fqt":1,"beg":today,"end":today}
    try:
        resp = requests.get(EASTMONEY_KLINE_URL, params=params, timeout=5)
        data = resp.json()
        if data["data"] and data["data"]["klines"]:
            parts = data["data"]["klines"][0].split(",")
            return {"day":parts[0],"open":float(parts[1]),"close":float(parts[2]),
                    "high":float(parts[3]),"low":float(parts[4]),"volume":float(parts[5])}
    except Exception as e:
        pass
    return None

def main():
    print(f"开始更新: {datetime.now()}")
    with open(INDEX_FILE, "r", encoding="utf-8") as f:
        html = f.read()
    match = re.search(r'const KLINE_DATA = (\{.*?\});', html, re.DOTALL)
    if not match:
        print("未找到K线数据")
        return
    kline_data = json.loads(match.group(1))
    codes = get_all_stock_codes()
    today = datetime.now().strftime("%Y-%m-%d")
    updated = 0
    for i, code in enumerate(codes):
        if i % 200 == 0:
            print(f"进度: {i}/{len(codes)}")
        k = get_today_kline(code)
        if k and k["day"] == today:
            if code not in kline_data:
                kline_data[code] = []
            if not any(x["day"] == today for x in kline_data[code]):
                kline_data[code].append(k)
                if len(kline_data[code]) > 250:
                    kline_data[code] = kline_data[code][-250:]
                updated += 1
    print(f"更新了{updated}只股票")
    if updated > 0:
        new_str = f"const KLINE_DATA = {json.dumps(kline_data, ensure_ascii=False)};"
        new_html = html.replace(match.group(0), new_str)
        with open(INDEX_FILE, "w", encoding="utf-8") as f:
            f.write(new_html)
        print("更新完成！")

if __name__ == "__main__":
    main()                
