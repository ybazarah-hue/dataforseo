#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
متتبّع ترتيب الموقع في جوجل (Daily SERP Rank Tracker)
- يبحث عن كلمات مفتاحية محددة في جوجل
- يحدد ترتيب الدومين المستهدف (الصفحة + الترتيب داخل الصفحة + الترتيب الكلي)
- يلتقط سكرين شوت للصفحة التي ظهر فيها الموقع
- يكتب النتائج في Google Sheet داخل تبويب (sheet) جديد باسم تاريخ اليوم
يعمل عبر ScrapingBee (Google Search API + Screenshot API) ومجدول عبر GitHub Actions.
"""

import os
import re
import sys
import json
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, quote_plus
from zoneinfo import ZoneInfo

import requests
import yaml
import gspread
from google.oauth2.service_account import Credentials

RIYADH = ZoneInfo("Asia/Riyadh")
SB_BASE = "https://app.scrapingbee.com/api/v1"
SB_GOOGLE = "https://app.scrapingbee.com/api/v1/store/google"


# ----------------------------- إعدادات وبيئة -----------------------------
def env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and not v:
        sys.exit(f"❌ متغيّر بيئة مفقود: {name}")
    return v


def load_config():
    cfg = {}
    p = Path("config.yaml")
    if p.exists():
        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    # السماح بتمرير الكلمات عبر Secret بدل ملف الإعداد (للخصوصية)
    kw_json = os.environ.get("KEYWORDS_JSON")
    if kw_json:
        try:
            data = json.loads(kw_json)
            if isinstance(data, dict):
                cfg.update(data)
            elif isinstance(data, list):
                cfg["keywords"] = data
        except json.JSONDecodeError:
            cfg["keywords"] = [k.strip() for k in re.split(r"[\n,]", kw_json) if k.strip()]

    if os.environ.get("TARGET_DOMAIN"):
        cfg["target_domain"] = os.environ["TARGET_DOMAIN"]

    cfg.setdefault("country_code", "sa")
    cfg.setdefault("language", "ar")
    cfg.setdefault("max_pages", 5)

    if not cfg.get("target_domain") or not cfg.get("keywords"):
        sys.exit("❌ لازم config يحتوي على target_domain و keywords")

    # تنظيف الكلمات: إزالة المسافات الزائدة وحذف التكرار مع الحفاظ على الترتيب
    seen_kw, clean = set(), []
    for k in cfg["keywords"]:
        k = str(k).strip()
        if k and k not in seen_kw:
            seen_kw.add(k)
            clean.append(k)
    cfg["keywords"] = clean
    return cfg


def norm_domain(d):
    d = (d or "").strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = d.split("/")[0]
    return d.replace("www.", "")


def host_of(url):
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""


# ----------------------------- نداءات ScrapingBee -----------------------------
def sb_google(api_key, keyword, country, language, page):
    """يرجّع قائمة النتائج العضوية (organic_results) لصفحة محددة."""
    params = {
        "api_key": api_key,
        "search": keyword,
        "country_code": country,
        "language": language,
        "page": page,
    }
    for attempt in range(3):
        try:
            r = requests.get(SB_GOOGLE, params=params, timeout=120)
            if r.status_code == 200:
                data = r.json()
                body = data.get("body", data)
                return body.get("organic_results", []) or []
            print(f"  ⚠️ بحث (محاولة {attempt+1}) كود {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"  ⚠️ خطأ بحث (محاولة {attempt+1}): {e}")
        time.sleep(3 * (attempt + 1))
    return []


def google_search_url(keyword, country, language, page):
    """رابط بحث جوجل المباشر — نظيف ومحايد (gl=الدولة, hl=اللغة, pws=0 يلغي التخصيص)."""
    start = (page - 1) * 10
    return (
        f"https://www.google.com/search?q={quote_plus(keyword)}"
        f"&gl={country}&hl={language}&start={start}&pws=0"
    )


def sb_screenshot(api_key, keyword, country, language, page, out_path):
    """يلتقط سكرين شوت لصفحة نتائج جوجل المطلوبة ويحفظها PNG.

    ScrapingBee يتطلب custom_google=true لاستهداف جوجل (يكلّف ~20 كريدت/صورة).
    يطبع رسالة الخطأ الكاملة عند الفشل لتسهيل التشخيص.
    """
    search_url = google_search_url(keyword, country, language, page)
    params = {
        "api_key": api_key,
        "url": search_url,
        "custom_google": "true",      # مطلوب لاستهداف جوجل
        "render_js": "true",
        "screenshot_full_page": "true",
        "country_code": country,
        "window_width": "1366",
        "wait": "3500",
    }
    for attempt in range(3):
        try:
            r = requests.get(SB_BASE, params=params, timeout=200)
            ct = r.headers.get("content-type", "")
            is_png = r.content[:8] == b"\x89PNG\r\n\x1a\n"
            if r.status_code == 200 and ("image" in ct or is_png):
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(r.content)
                return True
            print(f"  ⚠️ سكرين شوت (محاولة {attempt+1}) كود {r.status_code} نوع {ct} | {r.text[:300]}")
        except Exception as e:
            print(f"  ⚠️ خطأ سكرين شوت (محاولة {attempt+1}): {e}")
        time.sleep(3 * (attempt + 1))
    return False


# ----------------------------- كتابة Google Sheet -----------------------------
def write_sheet(sa_json, sheet_id, date_str, rows, now):
    creds = Credentials.from_service_account_info(
        json.loads(sa_json),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)

    title = date_str
    if title in [w.title for w in sh.worksheets()]:
        title = f"{date_str} ({now.strftime('%H%M')})"

    ws = sh.add_worksheet(title=title, rows=len(rows) + 6, cols=11)

    headers = [
        "#", "الكلمة المفتاحية", "الحالة", "الصفحة", "الترتيب في الصفحة",
        "الترتيب الكلي", "الرابط الظاهر", "المركز الأول (منافس)",
        "رابط البحث (تحقّق يدوي)", "وقت الفحص",
    ]
    values = [[f"تقرير ترتيب الموقع في جوجل — {date_str}"]]
    values.append(headers)
    values.extend(rows)
    ws.update(range_name="A1", values=values, value_input_option="USER_ENTERED")

    sid = ws.id
    body = {"requests": [
        # اتجاه الصفحة من اليمين لليسار
        {"updateSheetProperties": {
            "properties": {"sheetId": sid, "rightToLeft": True},
            "fields": "rightToLeft"}},
        # تجميد أول صفّين
        {"updateSheetProperties": {
            "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 2}},
            "fields": "gridProperties.frozenRowCount"}},
        # تنسيق صف العناوين
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": 2},
            "cell": {"userEnteredFormat": {
                "backgroundColor": {"red": 0.13, "green": 0.30, "blue": 0.45},
                "horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE",
                "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}}}},
            "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,textFormat)"}},
        # محاذاة عمودية لصفوف البيانات + التفاف النص
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 2, "endRowIndex": 2 + len(rows)},
            "cell": {"userEnteredFormat": {"verticalAlignment": "MIDDLE", "wrapStrategy": "WRAP"}},
            "fields": "userEnteredFormat(verticalAlignment,wrapStrategy)"}},
        # عرض عمود الكلمة المفتاحية
        {"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2},
            "properties": {"pixelSize": 220}, "fields": "pixelSize"}},
        # عرض عمود الرابط
        {"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 6, "endIndex": 7},
            "properties": {"pixelSize": 320}, "fields": "pixelSize"}},
        # عرض عمود رابط البحث (تحقّق يدوي)
        {"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 8, "endIndex": 9},
            "properties": {"pixelSize": 170}, "fields": "pixelSize"}},
    ]}
    sh.batch_update(body)
    print(f"✅ تم إنشاء التبويب: {title}")


# ----------------------------- المنطق الرئيسي -----------------------------
def main():
    api_key = env("SCRAPINGBEE_API_KEY", required=True)
    sa_json = env("GOOGLE_SERVICE_ACCOUNT_JSON", required=True)
    cfg = load_config()
    sheet_id = env("SHEET_ID") or cfg.get("sheet_id")
    if not sheet_id:
        sys.exit("❌ متغيّر SHEET_ID مفقود")

    target = norm_domain(cfg["target_domain"])
    keywords = cfg["keywords"]
    country = cfg["country_code"]
    language = cfg["language"]
    max_pages = int(cfg["max_pages"])

    now = datetime.now(RIYADH)
    date_str = now.strftime("%Y-%m-%d")

    # ملف نبضة يُحدّث كل تشغيل — يبقي جدولة GitHub نشطة (تتعطّل بعد 60 يوم خمول)
    Path("last_run.txt").write_text(
        now.strftime("%Y-%m-%d %H:%M (Asia/Riyadh)"), encoding="utf-8")

    print(f"🎯 الدومين المستهدف: {target} | الدولة: {country} | اللغة: {language} | الصفحات: {max_pages}")
    rows = []
    for i, kw in enumerate(keywords, start=1):
        found = None
        top_competitor = ""
        seen = 0
        for page in range(1, max_pages + 1):
            results = sb_google(api_key, kw, country, language, page)
            if page == 1 and results:
                top_competitor = host_of(results[0].get("url") or results[0].get("link") or "")
            for idx, res in enumerate(results):
                u = res.get("url") or res.get("link") or ""
                if target and target in host_of(u):
                    found = {
                        "page": page,
                        "rank_in_page": idx + 1,
                        "abs_pos": seen + idx + 1,
                        "url": u,
                    }
                    break
            if found:
                break
            seen += len(results)
            time.sleep(1)

        if found:
            status = "✅ ظهر"
            page_v, rank_v, abs_v, url_v = found["page"], found["rank_in_page"], found["abs_pos"], found["url"]
            shot_page = found["page"]
        else:
            status = f"❌ ما ظهر ضمن أول {max_pages} صفحات"
            page_v = rank_v = abs_v = url_v = "—"
            shot_page = 1

        # رابط بحث جوجل لنفس الصفحة — للتحقق اليدوي في نافذة خاصة (Incognito)
        verify_url = google_search_url(kw, country, language, shot_page)
        verify_cell = f'=HYPERLINK("{verify_url}","🔎 افتح بحث جوجل")'

        rows.append([i, kw, status, page_v, rank_v, abs_v, url_v,
                     top_competitor or "—", verify_cell, now.strftime("%H:%M")])
        print(f"[{i}/{len(keywords)}] {kw} → {status}"
              + (f" (صفحة {page_v}، ترتيب كلي {abs_v})" if found else ""))

    write_sheet(sa_json, sheet_id, date_str, rows, now)
    print("🎉 تم بنجاح.")


if __name__ == "__main__":
    main()
