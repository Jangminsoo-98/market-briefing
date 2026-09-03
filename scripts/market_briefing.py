"""
무료(비용 $0) 증시 브리핑 자동 생성기.
Windows 작업 스케줄러가 하루 세 번 이 스크립트를 실행한다:
  06:00 mode=yesterday  전일 마감 요약만 갱신
  08:30 mode=today      국내 지수/핵심종목 오늘 전망 갱신 (국내장 개장 30분 전)
  22:00 mode=usopen     해외 지수/핵심종목 실시간 재수집 + 다우·S&P500·나스닥 오늘 전망 예측
                         (미국장 개장 30분 전). 국내(kr/stocks) 쪽은 건드리지 않고 "오늘" doc의
                         us/us_stocks만 패치한다. 미국 서머타임이 바뀌면(3월/11월) 22:00이 아니라
                         23:00이어야 하므로 반기마다 이 트리거 시각을 수동으로 조정해줘야 한다.
today/usopen 모드는 실시간 수치를 그대로 보여주는 대신, 그 시점까지의 종가 + 뉴스 헤드라인을
근거로 Gemini가 상승/하락/보합 방향을 예측한다.
지수: 네이버페이 증권 폴링 API, Yahoo Finance 차트 API (무료, 키 불필요).
뉴스 요약: Google Gemini 무료 티어 API (gemini_key.txt 에 키가 있을 때만 사용, 없으면
규칙기반 정리로 자동 폴백 -- 항상 동작은 하도록 설계).
결과는 market_briefing_data.json / market_briefing_history.json 에 누적 저장되고,
market_briefing.html 로 정적 렌더링된다.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

KST = timezone(timedelta(hours=9))
NY = ZoneInfo("America/New_York")


def session_tag(now, open_t, close_t):
    """세 상태: 개장 전=SOON, 거래 중(open_t~close_t)=OPEN, 마감 후/주말=END."""
    if now.weekday() >= 5:
        return "END"
    if now < open_t:
        return "SOON"
    return "OPEN" if now <= close_t else "END"


def domestic_session_tag():
    """코스피/코스닥 실제 거래 시간: 평일 09:00~15:30 KST."""
    now = datetime.now(KST)
    open_t = now.replace(hour=9, minute=0, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return session_tag(now, open_t, close_t)


def us_session_tag():
    """뉴욕증시 정규장: 09:30~16:00 America/New_York (서머타임은 zoneinfo가 알아서 반영)."""
    now = datetime.now(NY)
    open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return session_tag(now, open_t, close_t)


UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) MarketBriefingBot/1.0"}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
DATA_PATH = os.path.join(ROOT_DIR, "market_briefing_data.json")
HISTORY_PATH = os.path.join(ROOT_DIR, "market_briefing_history.json")
HTML_PATH = os.path.join(ROOT_DIR, "market_briefing.html")
GEMINI_KEY_PATH = os.path.join(BASE_DIR, "gemini_key.txt")
GEMINI_MODEL = "gemini-flash-lite-latest"

# 로컬 PC(Desktop/Claude)에서는 사이트 저장소가 ROOT_DIR 밑의 별도 폴더(market-briefing-site)다.
# GitHub Actions에서는 이 스크립트 자체가 그 사이트 저장소 안(scripts/market_briefing.py)에 있어서
# ROOT_DIR이 곧 저장소 루트다 -- 그런 하위 폴더가 없으면 ROOT_DIR 자신을 사이트로 취급한다.
_NESTED_SITE_DIR = os.path.join(ROOT_DIR, "market-briefing-site")
SITE_DIR = _NESTED_SITE_DIR if os.path.isdir(_NESTED_SITE_DIR) else ROOT_DIR
SITE_INDEX_PATH = os.path.join(SITE_DIR, "index.html")

SRC_NAVER = "네이버페이 증권"
SRC_YAHOO = "Yahoo Finance"

STOCK_LIST = [
    ("005930", "삼성전자"),
    ("000660", "SK하이닉스"),
    ("373220", "LG에너지솔루션"),
    ("207940", "삼성바이오로직스"),
    ("005380", "현대차"),
]

US_STOCK_LIST = [
    ("AAPL", "애플"),
    ("MSFT", "마이크로소프트"),
    ("NVDA", "엔비디아"),
    ("AMZN", "아마존"),
    ("GOOGL", "구글"),
]


# ---------------------------------------------------------------- fetch ----

def fetch_json(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or UA, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        # 기본 str(e)는 "HTTP Error 400: Bad Request"처럼 상태줄만 나와서 원인 파악이 안 된다 --
        # 실제 원인(예: API 키 무효, IP 제한 등)은 보통 응답 본문 JSON에 있으므로 그걸 붙여서 다시 던진다.
        body = e.read().decode("utf-8", "replace")[:500]
        raise urllib.error.HTTPError(e.url, e.code, f"{e.reason} - {body}", e.headers, None) from None


def fetch_bytes(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=12) as r:
        return r.read()


def to_float(s):
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def get_kr_indices():
    items = []
    raw_values = {}
    status = None
    try:
        data = fetch_json(
            "https://polling.finance.naver.com/api/realtime/domestic/index/KOSPI,KOSDAQ"
        )
        names = {"KOSPI": "코스피", "KOSDAQ": "코스닥"}
        for item in data.get("datas", []):
            code = item.get("itemCode")
            name = names.get(code, code)
            close_val = to_float(item.get("closePriceRaw") or item.get("closePrice"))
            change_val = to_float(item.get("compareToPreviousClosePriceRaw"))
            ratio_val = to_float(item.get("fluctuationsRatioRaw") or item.get("fluctuationsRatio"))
            status = item.get("marketStatus") or status
            items.append({
                "label": name,
                "pct": ratio_val,
                "value": f"{close_val:,.2f}" if close_val is not None else None,
                "change": f"{change_val:+,.2f}" if change_val is not None else None,
            })
            if close_val is not None:
                raw_values[code.lower()] = close_val
            traded_at = item.get("localTradedAt")
            if traded_at:
                # 이 값이 실제로 어느 거래일 것인지 -- 스크립트가 도는 시각(예: 개장 전 06:00)의
                # 달력 날짜가 아니라 이 날짜를 히스토리 키로 써야 예측/실제 비교가 하루 밀리지 않는다.
                raw_values["trade_date"] = traded_at[:10]
    except Exception:
        items = [
            {"label": "코스피", "pct": None, "value": None, "change": None},
            {"label": "코스닥", "pct": None, "value": None, "change": None},
        ]
    return items, raw_values, status


def get_us_indices():
    tickers = [("^DJI", "다우존스"), ("^GSPC", "S&P500"), ("^IXIC", "나스닥")]
    items = []
    for code, name in tickers:
        try:
            q = urllib.parse.quote(code)
            data = fetch_json(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{q}?interval=1d&range=5d"
            )
            meta = data["chart"]["result"][0]["meta"]
            price = meta.get("regularMarketPrice")
            prev = meta.get("chartPreviousClose") or meta.get("previousClose")
            if price is not None and prev:
                change = price - prev
                pct = change / prev * 100
                items.append({
                    "label": name, "pct": round(pct, 2),
                    "value": f"{price:,.2f}", "change": f"{change:+,.2f}",
                })
            else:
                items.append({"label": name, "pct": None, "value": None, "change": None})
        except Exception:
            items.append({"label": name, "pct": None, "value": None, "change": None})
    return items


def get_us_stock_quotes(stock_list):
    items = []
    for code, name in stock_list:
        try:
            q = urllib.parse.quote(code)
            data = fetch_json(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{q}?interval=1d&range=5d"
            )
            meta = data["chart"]["result"][0]["meta"]
            price = meta.get("regularMarketPrice")
            prev = meta.get("chartPreviousClose") or meta.get("previousClose")
            if price is not None and prev:
                change = price - prev
                pct = change / prev * 100
                items.append({
                    "code": code, "label": name, "pct": round(pct, 2),
                    "value": f"${price:,.2f}", "change": f"{change:+,.2f}",
                })
            else:
                items.append({"code": code, "label": name, "pct": None, "value": None, "change": None})
        except Exception:
            items.append({"code": code, "label": name, "pct": None, "value": None, "change": None})
    return items


def get_stock_quotes(stock_list):
    codes = ",".join(code for code, _ in stock_list)
    by_code = {}
    try:
        data = fetch_json(f"https://polling.finance.naver.com/api/realtime/domestic/stock/{codes}")
        by_code = {d.get("itemCode"): d for d in data.get("datas", [])}
    except Exception:
        pass

    items = []
    for code, name in stock_list:
        d = by_code.get(code)
        if not d:
            items.append({"code": code, "label": name, "pct": None, "value": None, "change": None})
            continue
        close_val = to_float(d.get("closePriceRaw"))
        change_val = to_float(d.get("compareToPreviousClosePriceRaw"))
        ratio_val = to_float(d.get("fluctuationsRatioRaw") or d.get("fluctuationsRatio"))
        items.append({
            "code": code, "label": name, "pct": ratio_val,
            "value": f"{close_val:,.0f}원" if close_val is not None else None,
            "change": f"{change_val:+,.0f}" if change_val is not None else None,
        })
    return items


def get_stock_news(items, api_key):
    """종목별 헤드라인을 가져와 '왜 올랐는지/내렸는지' 한 문장 이유로 바꾸고(배치 1회 Gemini 호출)
    원문 기사 링크를 붙인다. AI 키가 없거나 실패하면 헤드라인 원문 제목을 이유로 그대로 쓴다."""
    headline_map, sources = {}, []
    for it in items:
        heads = fetch_headlines(f"{it['label']} 주가", n=2)
        if heads:
            headline_map[it["code"]] = heads[0]
            if heads[0]["source"] and heads[0]["source"] not in sources:
                sources.append(heads[0]["source"])
        else:
            headline_map[it["code"]] = None

    reasons = explain_stock_moves(items, headline_map, api_key) or {}
    news_map = {}
    for it in items:
        h = headline_map.get(it["code"])
        news_map[it["code"]] = (
            {"reason": reasons.get(it["code"]) or h["title"], "link": h.get("link")} if h else None
        )
    return news_map, sources


def fetch_headlines(query, n=6):
    out = []
    try:
        q = urllib.parse.quote(f"{query} when:1d")
        url = f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
        raw = fetch_bytes(url)
        root = ET.fromstring(raw)
        for it in root.findall(".//item")[:n]:
            title_el = it.find("title")
            title = (title_el.text or "").strip() if title_el is not None else ""
            source_el = it.find("source")
            source = source_el.text.strip() if source_el is not None and source_el.text else ""
            title = title.rsplit(" - ", 1)[0] if source and title.endswith(f"- {source}") else title
            link_el = it.find("link")
            link = link_el.text.strip() if link_el is not None and link_el.text else None
            if title:
                out.append({"title": title, "source": source, "link": link})
    except Exception:
        pass
    return out


# ------------------------------------------------------------- gemini -----

def load_gemini_key():
    env_key = os.environ.get("GEMINI_API_KEY")
    if env_key:
        return env_key.strip()
    if os.path.exists(GEMINI_KEY_PATH):
        try:
            with open(GEMINI_KEY_PATH, "r", encoding="utf-8") as f:
                key = f.read().strip()
                return key or None
        except Exception:
            return None
    return None


def gemini_summarize(prompt, api_key):
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
        f"?key={urllib.parse.quote(api_key)}"
    )
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 300},
    }).encode("utf-8")
    data = fetch_json(url, data=body, headers={"Content-Type": "application/json"})
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()


def clean_title(title):
    return re.sub(r"^\[[^\]]+\]\s*", "", title).strip()


def fallback_summary(heads):
    if not heads:
        return "관련 뉴스를 찾지 못했습니다."
    return " / ".join(clean_title(h["title"]) for h in heads[:3])


def summarize_group(scope_label, when_label, heads, api_key):
    if not heads:
        return "관련 뉴스를 찾지 못했습니다.", False
    headline_text = "\n".join(f"- {h['title']}" for h in heads)
    prompt = (
        f"다음은 {when_label} {scope_label} 증시 관련 뉴스 헤드라인입니다. "
        "이 헤드라인에 있는 사실만 근거로, 핵심 내용을 한국어 2문장(100자 내외)으로 자연스럽게 요약하세요. "
        "헤드라인에 없는 내용을 추측하거나 추가하지 마세요. 기호나 따옴표 없이 문장으로만 답하세요.\n\n"
        f"{headline_text}"
    )
    if api_key:
        for attempt in range(3):
            try:
                text = gemini_summarize(prompt, api_key)
                if text:
                    return text, True
                break
            except Exception as e:
                print(f"  [gemini] {scope_label} 요약 시도 {attempt + 1}/3 실패: {type(e).__name__}: {e}")
                if attempt < 2:
                    time.sleep(3)
    return fallback_summary(heads), False


def fetch_headlines_with_fallback(queries, n=6):
    """Try each query in order until one returns results -- an overly specific
    Google News query can legitimately match zero articles."""
    for q in queries:
        heads = fetch_headlines(q, n=n)
        if heads:
            return heads
    return []


def build_news(mode, api_key):
    when_label = "어제 하루" if mode == "yesterday" else "오늘 아침 기준"
    if mode == "yesterday":
        domestic_qs = ["코스피 코스닥 마감 특징주", "코스피 코스닥 특징주", "코스피"]
        overseas_qs = ["뉴욕증시 다우 나스닥 국제유가 환율", "뉴욕증시 다우 나스닥"]
    else:
        domestic_qs = ["코스피 코스닥 특징주", "코스피 전망", "코스피"]
        overseas_qs = ["뉴욕증시 다우 나스닥 FOMC 국제유가", "뉴욕증시 다우 나스닥"]

    domestic_heads = fetch_headlines_with_fallback(domestic_qs, n=6)
    overseas_heads = fetch_headlines_with_fallback(overseas_qs, n=6)

    sources = []
    for h in domestic_heads + overseas_heads:
        if h["source"] and h["source"] not in sources:
            sources.append(h["source"])

    domestic_summary, dom_ai = summarize_group("국내", when_label, domestic_heads, api_key)
    overseas_summary, over_ai = summarize_group("해외(미국)", when_label, overseas_heads, api_key)
    used_ai = dom_ai and over_ai
    return (
        {"domestic": domestic_summary, "overseas": overseas_summary},
        sources,
        used_ai,
        domestic_heads,
        overseas_heads,
    )


def extract_json(text):
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object found in response")
    return json.loads(text[start:end + 1])


def predict_outlook(kr_items, us_items, domestic_heads, overseas_heads, api_key):
    """AI 방향성 전망 (참고용) -- 전일 국내 지수 + 전일밤 미국 지수 + 오늘 뉴스 헤드라인을
    근거로 코스피/코스닥의 오늘 예상 방향(상승/하락/보합)과 근거를 판단하게 한다."""
    if not api_key:
        return None
    kr_ctx = "; ".join(
        f"{it['label']} 전일 종가 {it['value']} (전일 등락률 {it['pct']:+.2f}%)"
        for it in kr_items if it.get("pct") is not None
    )
    us_ctx = "; ".join(
        f"{it['label']} {it['pct']:+.2f}%" for it in us_items if it.get("pct") is not None
    )
    heads_ctx = "\n".join(f"- {h['title']}" for h in (domestic_heads + overseas_heads)[:10])
    prompt = (
        "당신은 국내 증권 애널리스트입니다. 아래 정보를 참고해 '오늘' 코스피와 코스닥의 예상 방향을 판단하세요.\n\n"
        f"[전일 국내 지수 마감] {kr_ctx or '정보 없음'}\n"
        f"[전일 밤 미국 지수 등락률] {us_ctx or '정보 없음'}\n"
        f"[오늘 아침 관련 뉴스 헤드라인]\n{heads_ctx or '정보 없음'}\n\n"
        "코스피와 코스닥 각각에 대해 오늘 예상 등락률을 숫자(%, 소수점 첫째자리, 예: 0.4 또는 -0.7)로 추정하고, "
        "근거를 한국어 1문장(60자 내외)으로 설명하세요. 이것은 확정된 사실이 아니라 제공된 정보에 기반한 "
        "대략적인 추정치임을 감안해 보수적인 범위(-3.0~3.0)에서 답하세요.\n"
        "다른 설명 없이 반드시 아래 JSON 형식으로만 답하세요 (pct는 숫자, 따옴표 없이):\n"
        '{"kospi": {"pct": 0.4, "reason": "..."}, '
        '"kosdaq": {"pct": -0.7, "reason": "..."}}'
    )
    for attempt in range(3):
        try:
            raw = gemini_summarize(prompt, api_key)
            data = extract_json(raw)
            if "kospi" in data and "kosdaq" in data:
                return data
            break
        except Exception as e:
            print(f"  [gemini] 전망 예측 시도 {attempt + 1}/3 실패: {type(e).__name__}: {e}")
            if attempt < 2:
                time.sleep(3)
    return None


def fetch_us_preopen_heads(n=6):
    return fetch_headlines_with_fallback(
        ["뉴욕증시 선물 개장 전망", "미국 증시 선물 FOMC 국제유가", "뉴욕증시 전망"], n=n
    )


def predict_us_outlook(us_items, heads, api_key):
    """AI 방향성 전망 (참고용) -- 미국장 개장 30분 전, 전일 뉴욕증시 종가 + 개장 전 저녁 뉴스
    헤드라인을 근거로 다우/S&P500/나스닥의 오늘 예상 방향을 판단하게 한다."""
    if not api_key:
        return None
    us_ctx = "; ".join(
        f"{it['label']} 전일 종가 {it['value']} (전일 등락률 {it['pct']:+.2f}%)"
        for it in us_items if it.get("pct") is not None
    )
    heads_ctx = "\n".join(f"- {h['title']}" for h in heads[:10])
    prompt = (
        "당신은 미국 증시 애널리스트입니다. 아래 정보를 참고해 '오늘' 개장할 뉴욕증시(다우존스, S&P500, "
        "나스닥)의 예상 방향을 판단하세요.\n\n"
        f"[전일 뉴욕증시 마감] {us_ctx or '정보 없음'}\n"
        f"[개장 전 관련 뉴스 헤드라인]\n{heads_ctx or '정보 없음'}\n\n"
        "다우존스, S&P500, 나스닥 각각에 대해 오늘 예상 등락률을 숫자(%, 소수점 첫째자리, 예: 0.4 또는 -0.7)로 "
        "추정하고, 근거를 한국어 1문장(60자 내외)으로 설명하세요. 이것은 확정된 사실이 아니라 제공된 정보에 "
        "기반한 대략적인 추정치임을 감안해 보수적인 범위(-3.0~3.0)에서 답하세요.\n"
        "다른 설명 없이 반드시 아래 JSON 형식으로만 답하세요 (pct는 숫자, 따옴표 없이):\n"
        '{"dow": {"pct": 0.4, "reason": "..."}, '
        '"sp500": {"pct": 0.3, "reason": "..."}, '
        '"nasdaq": {"pct": -0.2, "reason": "..."}}'
    )
    for attempt in range(3):
        try:
            raw = gemini_summarize(prompt, api_key)
            data = extract_json(raw)
            if "dow" in data and "sp500" in data and "nasdaq" in data:
                return data
            break
        except Exception as e:
            print(f"  [gemini] 미국장 전망 예측 시도 {attempt + 1}/3 실패: {type(e).__name__}: {e}")
            if attempt < 2:
                time.sleep(3)
    return None


def predict_stock_outlook(stock_items, news_map, api_key):
    """핵심 종목 5개에 대한 AI 방향성 전망 (참고용, 확정 사실 아님)."""
    if not api_key:
        return None
    lines = []
    for it in stock_items:
        if it.get("pct") is None:
            continue
        reason = (news_map.get(it["code"]) or {}).get("reason") or "관련 뉴스 없음"
        lines.append(
            f'{it["label"]}({it["code"]}): 전일 종가 {it["value"]} '
            f'(전일 등락률 {it["pct"]:+.2f}%) - 전일 등락 이유: {reason}'
        )
    if not lines:
        return None
    ctx = "\n".join(lines)
    keys = ", ".join(f'"{it["code"]}"' for it in stock_items)
    prompt = (
        "당신은 국내 증권 애널리스트입니다. 아래 핵심 종목들에 대해 각각 '오늘' 예상 등락률을 판단하세요.\n\n"
        f"{ctx}\n\n"
        "각 종목에 대해 오늘 예상 등락률을 숫자(%, 소수점 첫째자리, 예: 0.8 또는 -1.2)로 추정하고, "
        "근거를 한국어 1문장(50자 내외)으로 설명하세요. 확정된 사실이 아니라 제공된 정보에 기반한 대략적인 "
        "추정치임을 감안해 보수적인 범위(-5.0~5.0)에서 답하세요.\n"
        f"다른 설명 없이, 종목코드를 키로 하는 아래 JSON 형식으로만 답하세요 (키: {keys}, pct는 숫자, 따옴표 없이):\n"
        '{"005930": {"pct": 0.8, "reason": "..."}, ...}'
    )
    for attempt in range(3):
        try:
            raw = gemini_summarize(prompt, api_key)
            data = extract_json(raw)
            if any(it["code"] in data for it in stock_items):
                return data
            break
        except Exception as e:
            print(f"  [gemini] 종목 전망 시도 {attempt + 1}/3 실패: {type(e).__name__}: {e}")
            if attempt < 2:
                time.sleep(3)
    return None


def predict_us_stock_outlook(stock_items, news_map, api_key):
    """미국 핵심 종목 5개에 대한 AI 방향성 전망 (참고용, 확정 사실 아님) -- predict_stock_outlook과
    같은 구조지만 키가 종목코드(티커)이고 미국장 개장 전 컨텍스트를 쓴다."""
    if not api_key:
        return None
    lines = []
    for it in stock_items:
        if it.get("pct") is None:
            continue
        reason = (news_map.get(it["code"]) or {}).get("reason") or "관련 뉴스 없음"
        lines.append(
            f'{it["label"]}({it["code"]}): 전일 종가 {it["value"]} '
            f'(전일 등락률 {it["pct"]:+.2f}%) - 전일 등락 이유: {reason}'
        )
    if not lines:
        return None
    ctx = "\n".join(lines)
    keys = ", ".join(f'"{it["code"]}"' for it in stock_items)
    prompt = (
        "당신은 미국 증시 애널리스트입니다. 아래 미국 핵심 종목들에 대해 각각 오늘 개장 시 예상 등락률을 "
        "판단하세요.\n\n"
        f"{ctx}\n\n"
        "각 종목에 대해 오늘 예상 등락률을 숫자(%, 소수점 첫째자리, 예: 0.8 또는 -1.2)로 추정하고, "
        "근거를 한국어 1문장(50자 내외)으로 설명하세요. 확정된 사실이 아니라 제공된 정보에 기반한 대략적인 "
        "추정치임을 감안해 보수적인 범위(-5.0~5.0)에서 답하세요.\n"
        f"다른 설명 없이, 종목코드를 키로 하는 아래 JSON 형식으로만 답하세요 (키: {keys}, pct는 숫자, 따옴표 없이):\n"
        '{"AAPL": {"pct": 0.8, "reason": "..."}, ...}'
    )
    for attempt in range(3):
        try:
            raw = gemini_summarize(prompt, api_key)
            data = extract_json(raw)
            if any(it["code"] in data for it in stock_items):
                return data
            break
        except Exception as e:
            print(f"  [gemini] 미국 종목 전망 시도 {attempt + 1}/3 실패: {type(e).__name__}: {e}")
            if attempt < 2:
                time.sleep(3)
    return None


def explain_stock_moves(items, headline_map, api_key):
    """실제로 마감된 종목 등락에 대해, 관련 뉴스 헤드라인 근거로 '왜' 그렇게 움직였는지 한 문장씩
    만든다 (종목 개수만큼이 아니라 배치로 1회만 호출)."""
    if not api_key:
        return None
    lines = []
    for it in items:
        if it.get("pct") is None:
            continue
        h = headline_map.get(it["code"])
        headline = h["title"] if h else "관련 뉴스 없음"
        lines.append(f'{it["label"]}({it["code"]}): {it["pct"]:+.2f}% - 관련뉴스: {headline}')
    if not lines:
        return None
    ctx = "\n".join(lines)
    keys = ", ".join(f'"{it["code"]}"' for it in items)
    prompt = (
        "당신은 증권 애널리스트입니다. 아래는 종목들의 실제 등락률과 관련 뉴스 헤드라인입니다. "
        "각 종목이 왜 이렇게 움직였는지 헤드라인에 있는 사실에 근거해 한국어 1문장(50자 내외)으로 "
        "설명하세요. 헤드라인에 없는 내용을 추측하지 마세요.\n\n"
        f"{ctx}\n\n"
        f"다른 설명 없이, 종목코드를 키로 하는 아래 JSON 형식으로만 답하세요 (키: {keys}):\n"
        '{"005930": "...", ...}'
    )
    for attempt in range(3):
        try:
            raw = gemini_summarize(prompt, api_key)
            data = extract_json(raw)
            if any(it["code"] in data for it in items):
                return data
            break
        except Exception as e:
            print(f"  [gemini] 종목 이유 설명 시도 {attempt + 1}/3 실패: {type(e).__name__}: {e}")
            if attempt < 2:
                time.sleep(3)
    return None


def explain_index_moves(items, heads, scope_label, api_key):
    """실제로 마감된 지수 등락에 대해, 관련 뉴스 헤드라인 근거로 '왜' 그렇게 움직였는지 지수별로
    한 문장씩 만든다 (지수 개수만큼이 아니라 배치로 1회만 호출)."""
    if not api_key:
        return None
    valid = [it for it in items if it.get("pct") is not None]
    if not valid:
        return None
    ctx = "; ".join(f"{it['label']} {it['pct']:+.2f}%" for it in valid)
    heads_ctx = "\n".join(f"- {h['title']}" for h in heads[:8])
    keys = ", ".join(f'"{it["label"]}"' for it in valid)
    prompt = (
        f"당신은 증권 애널리스트입니다. 아래는 {scope_label} 지수들의 실제 등락률과 관련 뉴스 헤드라인입니다.\n\n"
        f"[등락률] {ctx}\n"
        f"[관련 뉴스 헤드라인]\n{heads_ctx or '정보 없음'}\n\n"
        "각 지수가 왜 이렇게 움직였는지 헤드라인에 있는 사실에 근거해 한국어 1문장(50자 내외)으로 "
        "설명하세요. 헤드라인에 없는 내용을 추측하지 마세요.\n"
        f"다른 설명 없이 반드시 아래 JSON 형식으로만 답하세요 (키: {keys}):\n"
        '{"코스피": "...", "코스닥": "..."}'
    )
    for attempt in range(3):
        try:
            raw = gemini_summarize(prompt, api_key)
            data = extract_json(raw)
            if any(it["label"] in data for it in valid):
                return data
            break
        except Exception as e:
            print(f"  [gemini] {scope_label} 지수 이유 설명 시도 {attempt + 1}/3 실패: {type(e).__name__}: {e}")
            if attempt < 2:
                time.sleep(3)
    return None


def build_doc(mode, api_key):
    kr_items, kr_raw, kr_status = get_kr_indices()
    us_items = get_us_indices()
    news, news_sources, used_ai, dom_heads, over_heads = build_news(mode, api_key)

    stock_items = get_stock_quotes(STOCK_LIST)
    stock_news, stock_news_sources = get_stock_news(stock_items, api_key)
    us_stock_items = get_us_stock_quotes(US_STOCK_LIST)
    us_stock_news, us_stock_news_sources = get_stock_news(us_stock_items, api_key)

    sources = [SRC_NAVER, SRC_YAHOO] + news_sources
    for s in stock_news_sources + us_stock_news_sources:
        if s not in sources:
            sources.append(s)

    outlook = None
    stock_outlook = None
    if mode == "today":
        outlook = predict_outlook(kr_items, us_items, dom_heads, over_heads, api_key)
        stock_outlook = predict_stock_outlook(stock_items, stock_news, api_key)

    # 실제로 왜 올랐/내렸는지는 '어제' 요약(전일 마감)에만 필요하다 -- 오늘 전망 쪽의 지수는
    # 항상 AI 예측(위 outlook)으로 대체 표시되므로 이 실데이터용 이유는 안 쓰인다.
    kr_reasons = us_reasons = kr_link = us_link = None
    if mode == "yesterday":
        kr_reasons = explain_index_moves(kr_items, dom_heads, "국내", api_key)
        us_reasons = explain_index_moves(us_items, over_heads, "해외(미국)", api_key)
        kr_link = dom_heads[0].get("link") if dom_heads else None
        us_link = over_heads[0].get("link") if over_heads else None

    return {
        "updated_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
        "kr": {"status": kr_status, "items": kr_items, "outlook": outlook, "reasons": kr_reasons, "link": kr_link},
        "us": {"items": us_items, "outlook": None, "reasons": us_reasons, "link": us_link},
        "stocks": {"items": stock_items, "news": stock_news, "outlook": stock_outlook},
        "us_stocks": {"items": us_stock_items, "news": us_stock_news, "outlook": None},
        "news": news,
        "sources": sources,
        "ai_summary": used_ai,
    }, kr_raw


# ------------------------------------------------------------- storage ----

def load_json(path):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def update_history(kr_raw, outlook=None):
    """실제 종가는 그 값이 실제로 속한 거래일(kr_raw['trade_date'], Naver의 localTradedAt에서
    뽑음)에 저장한다 -- 스크립트가 도는 시각(예: 개장 전 06:00, 08:50)의 달력 날짜를 그대로 쓰면
    장 열리기 전에는 항상 '어제 종가'가 잡히기 때문에, 그걸 '오늘' 날짜에 저장하면 예측/실제 비교가
    하루씩 밀린다. outlook(오늘 아침 AI가 예측한 오늘자 등락률)은 반대로 '예측이 가리키는 날짜',
    즉 지금 이 순간의 달력 날짜에 저장해야 한다 -- 아직 그 날의 실제 종가가 안 잡혀 있어도 나중에
    같은 날짜 키로 실제값이 채워지면 자연스럽게 합쳐진다."""
    if not kr_raw.get("kospi") or not kr_raw.get("kosdaq"):
        return
    history = load_json(HISTORY_PATH)

    trade_date = kr_raw.get("trade_date") or datetime.now(KST).strftime("%Y-%m-%d")
    actual_entry = history.get(trade_date, {})
    actual_entry["kospi"] = kr_raw["kospi"]
    actual_entry["kosdaq"] = kr_raw["kosdaq"]
    history[trade_date] = actual_entry

    if outlook:
        predict_date = datetime.now(KST).strftime("%Y-%m-%d")
        pred_entry = history.get(predict_date, {})
        kospi_pct = (outlook.get("kospi") or {}).get("pct")
        kosdaq_pct = (outlook.get("kosdaq") or {}).get("pct")
        if kospi_pct is not None:
            pred_entry["kospi_pred_pct"] = kospi_pct
        if kosdaq_pct is not None:
            pred_entry["kosdaq_pred_pct"] = kosdaq_pct
        history[predict_date] = pred_entry
    save_json(HISTORY_PATH, history)


# ---------------------------------------------------------------- html ----

def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def est_text_w(text, size=13, avg_char=0.62):
    return len(text) * size * avg_char


CHART_WIDTH = 664


def format_change_with_unit(it):
    """it['change']는 단위 없는 순수 증감값이고 it['value']에 단위가 붙어 있으므로
    (예: '253,500원', '$324.96'), value에서 단위를 읽어와 change에도 동일하게 붙여준다."""
    change, value = it.get("change"), it.get("value") or ""
    if not change:
        return None
    if value.endswith("원"):
        return f"{change}원"
    if value.startswith("$"):
        sign, digits = ("-", change[1:]) if change.startswith("-") else ("+", change.lstrip("+"))
        return f"{sign}${digits}"
    return change


def parse_amount(s):
    """'253,750원' / '$324.96' / '6,617.91' 같은 표시용 문자열에서 숫자만 뽑아낸다."""
    if not s:
        return None
    cleaned = re.sub(r"[^0-9+\-.]", "", s)
    try:
        return float(cleaned)
    except ValueError:
        return None


def format_like(value_str, num):
    if value_str.endswith("원"):
        return f"{num:,.0f}원"
    if value_str.startswith("$"):
        return f"${num:,.2f}"
    return f"{num:,.2f}"


def svg_dumbbell_group(items, predicted=False):
    """지수/종목의 등락을 %로 크기를 잡은 막대(전일 대비 상승=오른쪽/빨강, 하락=왼쪽/파랑)로 보여주고,
    막대 위에는 큰 글씨로 등락률을, 막대 아래에는 '전일값 → 현재값 (증감폭)'을 한 줄로 풀어서 보여준다.
    %는 종목 가격대와 무관하게 항상 비교 가능한 값이라 모든 행이 같은 중심축(cx)을 공유해도 된다.
    predicted=True면 AI가 등락률로부터 역산한 예상 도달가라는 뜻으로 점선/반투명/기울임으로 표시한다."""
    valid = [it for it in items if it.get("pct") is not None and it.get("value") and it.get("change")]
    if not valid:
        return svg_bar_group(items)

    max_abs = max(max(abs(it["pct"]) for it in valid), 0.3)
    row_h, bar_h = 64, 28
    label_w, gap, right_pad = 88, 18, 24
    width = CHART_WIDTH
    plot_left, plot_right = label_w + gap, width - right_pad
    half = (plot_right - plot_left) / 2
    cx = plot_left + half
    height = len(items) * row_h + 16

    rows = []
    for i, it in enumerate(items):
        y = 12 + i * row_h
        mid_y = y + bar_h / 2 + 5
        cat = f'<text x="{label_w - 10}" y="{mid_y:.1f}" text-anchor="end" class="chart-cat">{esc(it["label"])}</text>'

        pct = it.get("pct")
        curr_val = parse_amount(it.get("value"))
        change_val = parse_amount(it.get("change"))
        if pct is None or curr_val is None or change_val is None:
            rows.append(
                f'<g>{cat}<text x="{cx:.1f}" y="{mid_y:.1f}" text-anchor="middle" class="chart-muted">데이터 없음</text></g>'
            )
            continue

        prev_val = curr_val - change_val
        up = pct > 0
        color = "var(--chart-up)" if up else ("var(--chart-down)" if pct < 0 else "var(--chart-neutral)")
        change_disp = format_change_with_unit(it) or it["change"]

        bw = max(min(abs(pct) / max_abs * (half - 8), half - 8), 3)
        x = cx if pct >= 0 else cx - bw
        bar_style = f' fill-opacity="0.55" stroke="{color}" stroke-width="1.5" stroke-dasharray="5 3"' if predicted else ""
        title_suffix = " · AI 예측 도달가" if predicted else ""
        bar = (
            f'<rect x="{x:.1f}" y="{y}" width="{bw:.1f}" height="{bar_h}" rx="4" fill="{color}"{bar_style}>'
            f'<title>{esc(it["label"])} {it["value"]} ({change_disp}, {pct:+.2f}%){title_suffix}</title></rect>'
        )

        pct_text = f"{pct:+.2f}%"
        fits_inside = bw >= est_text_w(pct_text, size=15) + 20
        pred_cls = " predicted" if predicted else ""
        if fits_inside:
            lx, anchor, pct_class = (x + bw - 10) if up else (x + 10), ("end" if up else "start"), "chart-val-inside"
        else:
            lx, anchor, pct_class = (x + bw + 8) if up else (x - 8), ("start" if up else "end"), "chart-val"
        pct_label = (
            f'<text x="{lx:.1f}" y="{mid_y:.1f}" text-anchor="{anchor}" class="{pct_class}{pred_cls}" '
            f'style="font-size:15px">{pct_text}</text>'
        )

        detail = f'{esc(format_like(it["value"], prev_val))} → {esc(it["value"])} ({esc(change_disp)})'
        detail_label = (
            f'<text x="{plot_left:.1f}" y="{y + bar_h + 16:.1f}" text-anchor="start" class="chart-val-sub{pred_cls}">'
            f'{detail}</text>'
        )
        rows.append(f"<g>{cat}{bar}{pct_label}{detail_label}</g>")

    baseline = f'<line x1="{cx:.1f}" y1="2" x2="{cx:.1f}" y2="{height - 2}" class="chart-baseline"/>'
    return (
        f'<svg viewBox="0 0 {width} {height}" class="bar-chart" role="img" aria-label="전일 대비 가격 변동">'
        f"{baseline}{''.join(rows)}</svg>"
    )


def svg_bar_group(items, predicted=False):
    valid = [it for it in items if it.get("pct") is not None]
    if not valid:
        return '<p class="chart-empty">표시할 수치 데이터가 없습니다.</p>'

    max_abs = max(max(abs(it["pct"]) for it in valid), 0.3)
    row_h, bar_h = 48, 28
    label_w, gap, val_pad = 88, 18, 108
    width = CHART_WIDTH
    plot_left = label_w + gap
    plot_right = width - val_pad
    half = (plot_right - plot_left) / 2
    cx = plot_left + half
    height = len(items) * row_h + 16

    rows = []
    for i, it in enumerate(items):
        y = 12 + i * row_h
        mid_y = y + bar_h / 2 + 4.5
        cat = f'<text x="{label_w - 10}" y="{mid_y:.1f}" text-anchor="end" class="chart-cat">{esc(it["label"])}</text>'
        pct = it.get("pct")
        if pct is None:
            rows.append(f'<g>{cat}<text x="{cx:.1f}" y="{mid_y:.1f}" text-anchor="middle" class="chart-muted">데이터 없음</text></g>')
            continue

        bw = max(min(abs(pct) / max_abs * (half - 8), half - 8), 3)
        up = pct > 0
        color = "var(--chart-up)" if up else ("var(--chart-down)" if pct < 0 else "var(--chart-neutral)")
        x = cx if pct >= 0 else cx - bw
        bar_style = f' fill-opacity="0.55" stroke="{color}" stroke-width="1.5" stroke-dasharray="5 3"' if predicted else ""
        title_suffix = " (AI 예측)" if predicted else ""
        title_change = f" ({format_change_with_unit(it)})" if format_change_with_unit(it) else ""
        bar = f'<rect x="{x:.1f}" y="{y}" width="{bw:.1f}" height="{bar_h}" rx="4" fill="{color}"{bar_style}><title>{esc(it["label"])} {pct:+.2f}%{title_change}{title_suffix}</title></rect>'

        change_text = format_change_with_unit(it)
        if it.get("value"):
            line1 = it["value"]
            line2 = f"{change_text}, {pct:+.2f}%" if change_text else f"{pct:+.2f}%"
        else:
            line1 = f"{pct:+.2f}%"
            line2 = None

        est_w = max(est_text_w(line1, size=13 if line2 else 15), est_text_w(line2, size=11) if line2 else 0)
        fits_inside = bw >= est_w + 20
        if fits_inside:
            lx = (x + bw - 10) if up else (x + 10)
            anchor = "end" if up else "start"
        else:
            lx = (x + bw + 8) if up else (x - 8)
            anchor = "start" if up else "end"

        val_class1 = "chart-val-inside" if fits_inside else "chart-val"
        if predicted:
            val_class1 += " predicted"

        if line2:
            val_class2 = "chart-val-sub" + (" inside" if fits_inside else "") + (" predicted" if predicted else "")
            val = (
                f'<text x="{lx:.1f}" y="{mid_y - 6:.1f}" text-anchor="{anchor}" class="{val_class1}">{esc(line1)}</text>'
                f'<text x="{lx:.1f}" y="{mid_y + 8:.1f}" text-anchor="{anchor}" class="{val_class2}">{esc(line2)}</text>'
            )
        else:
            val = (
                f'<text x="{lx:.1f}" y="{mid_y:.1f}" text-anchor="{anchor}" class="{val_class1}" '
                f'style="font-size:15px">{esc(line1)}</text>'
            )

        rows.append(f"<g>{cat}{bar}{val}</g>")

    baseline = f'<line x1="{cx:.1f}" y1="2" x2="{cx:.1f}" y2="{height - 2}" class="chart-baseline"/>'
    return (
        f'<svg viewBox="0 0 {width} {height}" class="bar-chart" role="img" aria-label="지수 등락률">'
        f"{baseline}{''.join(rows)}</svg>"
    )


def predicted_target(base_value, pct):
    """AI가 준 예상 등락률(pct)을 전일 종가(base_value, 단위 포함 문자열)에 적용해
    예상 도달가와 증감폭 문자열을 역산한다."""
    base_val = parse_amount(base_value)
    if base_val is None or pct is None:
        return None, None
    predicted_val = base_val * (1 + pct / 100)
    change_val = predicted_val - base_val
    value_str = format_like(base_value, predicted_val)
    change_str = f"{change_val:+,.0f}" if base_value.endswith("원") else f"{change_val:+,.2f}"
    return value_str, change_str


def predicted_bar_block(outlook, keys, base_items=None, links=None):
    """AI가 추정한 등락률(pct)을 전일 종가에 적용해 예상 도달가까지 실제 데이터와 같은 막대
    형태로 보여주되, 점선/반투명/기울임으로 '확정 사실이 아닌 예측'임을 시각적으로 구분한다.
    근거 문장은 그래프 아래 목록으로 보여준다. links(선택)는 key -> 관련 기사 URL 매핑이다."""
    base_by_label = {it["label"]: it for it in (base_items or [])}
    links = links or {}
    items, reasons = [], []
    for key, name in keys:
        info = (outlook or {}).get(key) or {}
        pct = info.get("pct")
        try:
            pct = round(float(pct), 2) if pct is not None else None
        except (TypeError, ValueError):
            pct = None

        base_value = (base_by_label.get(name) or {}).get("value")
        value_str, change_str = predicted_target(base_value, pct) if base_value else (None, None)
        items.append({"label": name, "pct": pct, "value": value_str, "change": change_str})
        reasons.append((name, info.get("reason") or "근거 정보 없음", links.get(key)))

    chart = svg_dumbbell_group(items, predicted=True)

    def row(name, reason, link):
        link_html = f' <a href="{esc(link)}" target="_blank" rel="noopener">기사보기 ↗</a>' if link else ""
        return f'<li><strong>{esc(name)}</strong> · {esc(reason)}{link_html}</li>'

    reason_rows = "".join(row(name, reason, link) for name, reason, link in reasons)
    return (
        '<p class="outlook-disclaimer">AI 예측치 · 점선/반투명/기울임 표시 · 전일 종가에 예상 등락률을 '
        '적용해 역산한 참고용 도달가이며 확정된 사실이 아닙니다</p>'
        f"{chart}"
        f'<ul class="stock-news">{reason_rows}</ul>'
    )


def numeric_highlight_list(items, reasons=None, link=None):
    """지수 그룹(해외 지수, 전일 마감 국내 지수)에 붙일 한 줄 핵심 요약 목록. 숫자는 차트에 이미
    다 나와 있으니 여기서는 반복하지 않고 '왜'(explain_index_moves 결과)만 보여준다 -- AI 키가
    없어서 이유가 없을 때만 등락률로 폴백한다. link(관련 기사)가 있으면 끝에 붙인다."""
    reasons = reasons or {}
    rows = []
    for it in items:
        pct = it.get("pct")
        if pct is None:
            continue
        reason = reasons.get(it["label"])
        word = "상승" if pct > 0 else ("하락" if pct < 0 else "보합")
        text = reason or f"전일 대비 {pct:+.2f}% {word}"
        link_html = f' <a href="{esc(link)}" target="_blank" rel="noopener">기사보기 ↗</a>' if link else ""
        rows.append(f'<li><strong>{esc(it["label"])}</strong> · {esc(text)}{link_html}</li>')
    return f'<ul class="stock-news">{"".join(rows)}</ul>' if rows else ""


def chart_group_block(title, tag, items, summary_text, outlook=None, outlook_keys=None,
                       update_note=None, awaiting_prediction=False, pending_text=None,
                       reasons=None, link=None):
    tag_html = f'<span class="chart-tag tag-{tag.lower()}">{esc(tag)}</span>' if tag else ""
    note_html = f'<span class="chart-update-note">{esc(update_note)}</span>' if update_note else ""
    if outlook is not None:
        links = {key: link for key, _ in (outlook_keys or [])} if link else None
        body = predicted_bar_block(outlook, outlook_keys or [], base_items=items, links=links)
        highlight_html = ""
    elif awaiting_prediction:
        # outlook이 None인 이유는 둘 중 하나다: (1) 이 그룹의 예측은 아직 안 도는 트리거 몫이거나
        # (예: 해외 지수는 22:00), (2) 이미 돌았어야 할 트리거인데 Gemini 호출이 실패했거나. 어느
        # 쪽이든 어제 마감 실데이터를 예측인 척(과거형 문장으로) 보여주면 혼동만 주므로 빈 칸으로 둔다.
        if pending_text:
            body = f'<p class="chart-empty">{esc(pending_text)}</p>'
        else:
            when = update_note.replace("매일 ", "").replace(" 갱신", "") if update_note else "다음 갱신"
            body = f'<p class="chart-empty">아직 예측 전입니다 -- {esc(when)}에 AI 예측으로 업데이트됩니다.</p>'
        highlight_html = ""
        summary_text = None
    else:
        body = svg_dumbbell_group(items)
        highlight_html = numeric_highlight_list(items, reasons=reasons, link=link)
    summary_html = f'<p class="chart-summary">{esc(summary_text)}</p>' if summary_text else ""
    return f"""
      <div class="chart-group">
        <div class="chart-group-head">
          <h3>{esc(title)}{tag_html}</h3>
          {note_html}
        </div>
        {body}
        {highlight_html}
        {summary_html}
      </div>"""


def stock_group_block(title, items, news_map=None, outlook=None, tag=None,
                       update_note=None, awaiting_prediction=False, pending_text=None):
    outlook_keys = [(it["code"], it["label"]) for it in items]
    news_map = news_map or {}
    tag_html = f'<span class="chart-tag tag-{tag.lower()}">{esc(tag)}</span>' if tag else ""
    note_html = f'<span class="chart-update-note">{esc(update_note)}</span>' if update_note else ""
    if outlook is not None:
        links = {code: (news_map.get(code) or {}).get("link") for code, _ in outlook_keys}
        body = predicted_bar_block(outlook, outlook_keys, base_items=items, links=links)
        news_html = ""  # AI 예측의 근거 목록이 이미 뉴스 맥락을 반영하므로 중복 표시하지 않음
    elif awaiting_prediction:
        if pending_text:
            body = f'<p class="chart-empty">{esc(pending_text)}</p>'
        else:
            when = update_note.replace("매일 ", "").replace(" 갱신", "") if update_note else "다음 갱신"
            body = f'<p class="chart-empty">아직 예측 전입니다 -- {esc(when)}에 AI 예측으로 업데이트됩니다.</p>'
        news_html = ""
    else:
        body = svg_dumbbell_group(items)

        def row(it):
            entry = news_map.get(it["code"])
            if not entry:
                return ""
            link_html = (
                f' <a href="{esc(entry["link"])}" target="_blank" rel="noopener">기사보기 ↗</a>'
                if entry.get("link") else ""
            )
            return f'<li><strong>{esc(it["label"])}</strong> · {esc(entry["reason"])}{link_html}</li>'

        news_rows = "".join(row(it) for it in items)
        news_html = f'<ul class="stock-news">{news_rows}</ul>' if news_rows else ""
    return f"""
      <div class="chart-group">
        <div class="chart-group-head">
          <h3>{esc(title)}{tag_html}</h3>
          {note_html}
        </div>
        {body}
        {news_html}
      </div>"""


def svg_trend_chart(history):
    dates = sorted(history.keys())
    if len(dates) < 2:
        return (
            '<p class="chart-empty">최근 며칠간의 데이터가 쌓이면 코스피 · 코스닥 추세선이 여기 표시됩니다 '
            "(평일마다 자동 누적).</p>"
        )
    dates = dates[-14:]
    base_kospi = history[dates[0]]["kospi"]
    base_kosdaq = history[dates[0]]["kosdaq"]
    idx_kospi = [history[d]["kospi"] / base_kospi * 100 for d in dates]
    idx_kosdaq = [history[d]["kosdaq"] / base_kosdaq * 100 for d in dates]

    # 그날 아침 AI가 예측한 등락률을, 그 전날 실제 종가에 적용해 같은 지수화 스케일로 환산한다
    # (없는 날은 None) -- 실선(실제)과 겹쳐 그려서 예측이 얼마나 맞았는지 한눈에 비교할 수 있게 한다.
    def pred_series(field, base):
        out = [None] * len(dates)
        for i in range(1, len(dates)):
            prev, cur = history[dates[i - 1]], history[dates[i]]
            pct = cur.get(field)
            prev_val = prev.get("kospi" if field.startswith("kospi") else "kosdaq")
            if pct is not None and prev_val:
                out[i] = (prev_val * (1 + pct / 100)) / base * 100
        return out

    pred_kospi = pred_series("kospi_pred_pct", base_kospi)
    pred_kosdaq = pred_series("kosdaq_pred_pct", base_kosdaq)

    width, height = CHART_WIDTH, 190
    pad_l, pad_r, pad_t, pad_b = 40, 92, 16, 26
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    all_vals = idx_kospi + idx_kosdaq + [v for v in pred_kospi + pred_kosdaq if v is not None]
    v_min, v_max = min(all_vals), max(all_vals)
    v_span = max(v_max - v_min, 1)
    v_min -= v_span * 0.15
    v_max += v_span * 0.15
    v_span = v_max - v_min

    def xy(i, v):
        x = pad_l + (i / (len(dates) - 1)) * plot_w
        y = pad_t + plot_h - ((v - v_min) / v_span) * plot_h
        return x, y

    def path_for(vals):
        pts = [xy(i, v) for i, v in enumerate(vals)]
        d = f"M {pts[0][0]:.1f} {pts[0][1]:.1f} " + " ".join(f"L {x:.1f} {y:.1f}" for x, y in pts[1:])
        return d, pts

    kospi_d, kospi_pts = path_for(idx_kospi)
    kosdaq_d, kosdaq_pts = path_for(idx_kosdaq)

    def pred_markers(pred_vals, actual_vals, color, label):
        out = []
        for i, pv in enumerate(pred_vals):
            if pv is None:
                continue
            x, y = xy(i, pv)
            err = pred_vals[i] - actual_vals[i]
            out.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="var(--paper-raised)" stroke="{color}" '
                f'stroke-width="2" stroke-dasharray="2 2"><title>{label} {dates[i]} AI 예측 vs 실제 '
                f'(오차 {err:+.1f}pt)</title></circle>'
            )
        return "".join(out)

    pred_marks = (
        pred_markers(pred_kospi, idx_kospi, "var(--series-1)", "코스피")
        + pred_markers(pred_kosdaq, idx_kosdaq, "var(--series-2)", "코스닥")
    )
    has_pred = any(v is not None for v in pred_kospi + pred_kosdaq)
    grid = f'<line x1="{pad_l}" y1="{pad_t + plot_h}" x2="{pad_l + plot_w}" y2="{pad_t + plot_h}" class="chart-baseline"/>'
    date_labels = (
        f'<text x="{pad_l}" y="{height - 6}" text-anchor="start" class="chart-muted">{dates[0][5:]}</text>'
        f'<text x="{pad_l + plot_w}" y="{height - 6}" text-anchor="end" class="chart-muted">{dates[-1][5:]}</text>'
    )
    kospi_end, kosdaq_end = kospi_pts[-1], kosdaq_pts[-1]
    end_dots = (
        f'<circle cx="{kospi_end[0]:.1f}" cy="{kospi_end[1]:.1f}" r="4" fill="var(--series-1)" stroke="var(--paper-raised)" stroke-width="2"/>'
        f'<circle cx="{kosdaq_end[0]:.1f}" cy="{kosdaq_end[1]:.1f}" r="4" fill="var(--series-2)" stroke="var(--paper-raised)" stroke-width="2"/>'
        f'<text x="{kospi_end[0]+9:.1f}" y="{kospi_end[1]+4:.1f}" class="chart-val">코스피 {idx_kospi[-1]:.1f}</text>'
        f'<text x="{kosdaq_end[0]+9:.1f}" y="{kosdaq_end[1]+16:.1f}" class="chart-val">코스닥 {idx_kosdaq[-1]:.1f}</text>'
    )
    pred_legend = ' · <span style="border:1.5px dashed var(--muted);border-radius:50%;width:8px;height:8px;display:inline-block"></span> AI 예측' if has_pred else ""
    return (
        '<div class="chart-legend"><span class="dot series1"></span>코스피'
        f'<span class="dot series2"></span>코스닥{pred_legend}'
        f'<span class="chart-note">첫날({dates[0]})=100 기준 지수화</span></div>'
        f'<svg viewBox="0 0 {width} {height}" class="trend-chart" role="img" aria-label="코스피 코스닥 추세">'
        f"{grid}{date_labels}"
        f'<path d="{kospi_d}" class="trend-line series1"/>'
        f'<path d="{kosdaq_d}" class="trend-line series2"/>'
        f"{pred_marks}{end_dots}</svg>"
    )


def render_part(label, part_no, time_label, doc):
    if not doc:
        return f"""
  <section class="part">
    <div class="part-head">
      <div><p class="label">Part {part_no} · {time_label} 발행</p><h2>{label}</h2></div>
      <time></time>
    </div>
    <div class="part-body"><div class="empty">아직 발행된 브리핑이 없습니다.</div></div>
  </section>"""

    # kr/stocks의 예측은 08:30 트리거 자신이 만드는 값이라 "아직 도는 트리거가 안 왔다"는 없다 --
    # outlook이 None인데 part_no==2라면 Gemini 호출이 실패한 것이므로, 어제 실데이터를 예측인 척
    # (과거형 문장으로) 보여주는 대신 실패했다는 걸 명확히 알려준다.
    kr_pending = "AI 예측을 가져오지 못했습니다 (다음 자동 갱신 때 다시 시도합니다)." if part_no == 2 else None
    kr_block = chart_group_block(
        "국내 지수", domestic_session_tag() if part_no == 2 else None, doc["kr"]["items"],
        doc["news"]["domestic"], outlook=doc["kr"].get("outlook"),
        outlook_keys=[("kospi", "코스피"), ("kosdaq", "코스닥")],
        update_note="매일 08:30 갱신" if part_no == 2 else None,
        awaiting_prediction=(part_no == 2), pending_text=kr_pending,
        reasons=doc["kr"].get("reasons"), link=doc["kr"].get("link"),
    )
    us_block = chart_group_block(
        "해외 지수 (미국)", us_session_tag() if part_no == 2 else None, doc["us"]["items"], doc["news"]["overseas"],
        outlook=doc["us"].get("outlook"),
        outlook_keys=[("dow", "다우존스"), ("sp500", "S&P500"), ("nasdaq", "나스닥")],
        update_note="매일 22:00 갱신" if part_no == 2 else None,
        awaiting_prediction=(part_no == 2),
        reasons=doc["us"].get("reasons"), link=doc["us"].get("link"),
    )

    stocks = doc.get("stocks")
    stock_block = (
        stock_group_block(
            "국내 핵심 종목", stocks["items"], stocks["news"], outlook=stocks.get("outlook"),
            tag=domestic_session_tag() if part_no == 2 else None,
            update_note="매일 08:30 갱신" if part_no == 2 else None,
            awaiting_prediction=(part_no == 2), pending_text=kr_pending,
        )
        if stocks else ""
    )
    us_stocks = doc.get("us_stocks")
    us_stock_block = (
        stock_group_block(
            "해외 핵심 종목", us_stocks["items"], us_stocks["news"], outlook=us_stocks.get("outlook"),
            tag=us_session_tag() if part_no == 2 else None,
            update_note="매일 22:00 갱신" if part_no == 2 else None,
            awaiting_prediction=(part_no == 2),
        )
        if us_stocks else ""
    )
    sources = " · ".join(esc(s) for s in doc.get("sources", []))
    updated = esc(doc.get("updated_at", ""))
    ai_note = "" if doc.get("ai_summary") else '<p class="ai-note">※ AI 요약 키가 설정되지 않아 헤드라인을 간단히 정리한 버전입니다.</p>'

    return f"""
  <section class="part">
    <div class="part-head">
      <div><p class="label">Part {part_no} · {time_label} 발행</p><h2>{label}</h2></div>
      <time>{updated}</time>
    </div>
    <div class="part-body">
      <div class="chart-groups">{kr_block}{us_block}{stock_block}{us_stock_block}</div>
      {ai_note}
      <p class="sources">출처 · {sources}</p>
    </div>
  </section>"""


HTML_SHELL = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>데일리 마켓 브리핑(클라우드)</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Noto+Serif+KR:wght@500;600;700&family=IBM+Plex+Sans+KR:wght@400;500;600;700&family=IBM+Plex+Mono:wght@500;600;700&display=swap');
  :root {{
    --navy-950:#10141d; --navy-700:#2c3a52; --paper:#f4f5f1; --paper-raised:#fbfbf9;
    --line:#dee1da; --gold:#b3822c; --gold-soft:#eadfc4; --muted:#6b7280;
    --chart-up:#c33a2f; --chart-down:#2f5fb0; --chart-neutral:#9a9a92;
    --series-1:#2a78d6; --series-2:#eb6834;
    --font-display:'Noto Serif KR',serif; --font-body:'IBM Plex Sans KR',sans-serif;
    --font-mono:'IBM Plex Mono',monospace;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --navy-950:#eef0f4; --navy-700:#c7cedb; --paper:#12151c; --paper-raised:#191d26;
      --line:#2b303c; --gold:#d9ac54; --gold-soft:#3a3021; --muted:#8890a0;
      --chart-up:#e2695d; --chart-down:#6d9ceb; --chart-neutral:#7b7b74;
      --series-1:#3987e5; --series-2:#d95926; }}
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--paper); color:var(--navy-950); font-family:var(--font-body); line-height:1.6; }}
  .wrap {{ max-width:1440px; margin:0 auto; padding:48px 32px 80px; }}
  header.page {{ border-bottom:2px solid var(--navy-950); padding-bottom:20px; margin-bottom:32px; }}
  .eyebrow {{ font-family:var(--font-mono); font-size:12px; letter-spacing:.12em; color:var(--gold); text-transform:uppercase; margin:0 0 10px; }}
  h1.title {{ font-family:var(--font-display); font-weight:700; font-size:clamp(26px,5vw,36px); margin:0 0 8px; }}
  .subtitle {{ color:var(--muted); font-size:14px; margin:0; }}
  .update-schedule {{ list-style:none; margin:8px 0 0; padding:0; font-size:13px; color:var(--muted); font-family:var(--font-mono); }}
  .update-schedule li {{ margin:2px 0; }}

  .parts-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:28px; align-items:start; margin-bottom:28px; }}
  @media (max-width: 980px) {{
    .parts-grid {{ grid-template-columns:1fr; }}
  }}

  section.part {{ background:var(--paper-raised); border:1px solid var(--line); border-radius:4px; overflow:hidden; }}
  .part-head {{ display:flex; align-items:baseline; justify-content:space-between; gap:16px; padding:20px 24px;
    border-bottom:1px solid var(--line); background:linear-gradient(180deg,var(--gold-soft) 0%,transparent 100%); }}
  .part-head .label {{ font-family:var(--font-mono); font-size:11.5px; letter-spacing:.1em; text-transform:uppercase; color:var(--navy-700); margin:0 0 4px; }}
  .part-head h2 {{ font-family:var(--font-display); font-size:20px; font-weight:600; margin:0; }}
  .part-head time {{ font-family:var(--font-mono); font-size:12px; color:var(--muted); white-space:nowrap; }}
  .part-body {{ padding:22px 24px 26px; }}

  .chart-groups {{ display:flex; flex-direction:column; gap:18px; margin-bottom:14px; }}
  .chart-group {{ background:var(--paper); border:1px solid var(--line); border-radius:6px; padding:16px 18px 16px; }}
  .chart-group-head {{ display:flex; align-items:baseline; justify-content:space-between; gap:10px; flex-wrap:wrap; margin-bottom:10px; }}
  .chart-group-head h3 {{ margin:0; font-size:15px; font-weight:700; display:flex; align-items:center; gap:8px; }}
  .chart-tag {{ font-family:var(--font-mono); font-size:10.5px; font-weight:500; letter-spacing:.06em; color:var(--gold); border:1px solid var(--gold); border-radius:20px; padding:1px 8px; }}
  .chart-tag.tag-open {{ color:var(--chart-up); border-color:var(--chart-up); }}
  .chart-tag.tag-soon {{ color:var(--muted); border-color:var(--muted); }}
  .chart-tag.tag-end {{ color:var(--muted); border-color:var(--line); opacity:.7; }}
  .chart-update-note {{ font-family:var(--font-mono); font-size:11px; color:var(--muted); white-space:nowrap; margin-left:auto; }}
  .chart-summary {{ margin:12px 0 0; padding-top:12px; border-top:1px solid var(--line); font-size:14px; color:var(--navy-700); }}
  .bar-chart, .trend-chart {{ width:100%; height:auto; display:block; }}
  .chart-cat {{ font-size:13.5px; font-weight:600; fill:var(--navy-950); font-family:var(--font-body); }}
  .chart-val {{ font-size:13px; font-weight:700; fill:var(--navy-950); font-family:var(--font-mono); font-variant-numeric:tabular-nums; }}
  .chart-val-inside {{ font-size:13px; font-weight:700; fill:#fff; font-family:var(--font-mono); font-variant-numeric:tabular-nums; }}
  .chart-val-sub {{ font-size:11px; font-weight:500; fill:var(--muted); font-family:var(--font-mono); font-variant-numeric:tabular-nums; }}
  .chart-val-sub.inside {{ fill:rgba(255,255,255,0.85); }}
  .chart-muted {{ font-size:12px; fill:var(--muted); }}
  .chart-baseline {{ stroke:var(--line); stroke-width:1; }}
  .chart-empty {{ color:var(--muted); font-size:14px; margin:0; }}
  .chart-legend {{ display:flex; align-items:center; gap:14px; font-size:12px; color:var(--muted); margin:0 0 8px; font-family:var(--font-mono); }}
  .chart-legend .dot {{ display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:5px; }}
  .chart-legend .dot.series1 {{ background:var(--series-1); }}
  .chart-legend .dot.series2 {{ background:var(--series-2); }}
  .chart-note {{ margin-left:auto; }}

  .outlook-disclaimer {{ margin:0 0 8px; font-size:11px; color:var(--muted); font-style:italic; }}
  .chart-val.predicted, .chart-val-inside.predicted, .chart-val-sub.predicted {{ font-style:italic; }}

  .stock-news {{ margin:12px 0 0; padding-top:12px; border-top:1px solid var(--line); list-style:none; padding-left:0; }}
  .stock-news li {{ font-size:13px; color:var(--navy-700); margin-bottom:6px; }}
  .stock-news li:last-child {{ margin-bottom:0; }}
  .stock-news strong {{ color:var(--navy-950); font-weight:700; }}
  .trend-line {{ fill:none; stroke-width:2; stroke-linejoin:round; stroke-linecap:round; }}
  .trend-line.series1 {{ stroke:var(--series-1); }}
  .trend-line.series2 {{ stroke:var(--series-2); }}

  .ai-note {{ margin:0 0 10px; font-size:11.5px; color:var(--muted); font-style:italic; }}
  .sources {{ margin:14px 0 0; padding-top:10px; border-top:1px dashed var(--line); font-size:11.5px; color:var(--muted); font-family:var(--font-mono); }}
  .empty {{ color:var(--muted); font-size:14px; }}
  footer.page {{ margin-top:40px; padding-top:16px; border-top:1px solid var(--line); font-size:12px; color:var(--muted); font-family:var(--font-mono); }}
</style>
</head>
<body>
<div class="wrap">
  <header class="page">
    <p class="eyebrow">KOSPI · KOSDAQ · US MARKETS (CLOUD / FREE)</p>
    <h1 class="title">데일리 마켓 브리핑(클라우드)</h1>
    <p class="subtitle">GitHub Actions가 평일 하루 세 번 자동 생성 (마지막 갱신: {generated_at})</p>
    <ul class="update-schedule">
      <li>· 06:00 전일 마감 요약 (최근 AI 예측·실제 정확도)</li>
      <li>· 08:30 국내 지수·종목 오늘 전망</li>
      <li>· 22:00 해외 지수·종목 오늘 전망</li>
    </ul>
  </header>
  <div class="parts-grid">
{part_yesterday}
{part_today}
  </div>
  <section class="part" style="margin-bottom:28px;">
    <div class="part-head">
      <div><p class="label">AI 예측 · 실제 정확도</p><h2>최근 코스피 · 코스닥 흐름 (AI 예측 vs 실제)</h2></div>
      <time></time>
    </div>
    <div class="part-body">
      <p class="ai-note">매일 아침 AI가 예측한 등락률(점선 원)을 그날 실제 마감값(실선)과 겹쳐 보여줍니다 -- 원이 실선에 가까울수록 예측이 정확했다는 뜻입니다.</p>
      {trend_chart}
    </div>
  </section>
  <footer class="page">
    지수는 실시간 데이터, 뉴스 요약은 헤드라인 기반 AI(또는 규칙기반) 정리입니다. 투자 판단의 책임은 본인에게 있습니다.
  </footer>
</div>
<script>
(function() {{
  // 전일 마감 요약칸과 금일 전망칸은 내용 길이가 매일 달라서 순수 CSS로는 줄이 안 맞는다 --
  // 같은 순서의 chart-group끼리 실제 렌더된 높이를 재서 더 큰 쪽에 맞춰 min-height를 강제한다.
  function align() {{
    var cols = document.querySelectorAll('.parts-grid > section.part');
    if (cols.length < 2) return;
    var groupsA = cols[0].querySelectorAll('.chart-group');
    var groupsB = cols[1].querySelectorAll('.chart-group');
    var n = Math.min(groupsA.length, groupsB.length);
    var i;
    for (i = 0; i < n; i++) {{
      groupsA[i].style.minHeight = '';
      groupsB[i].style.minHeight = '';
    }}
    for (i = 0; i < n; i++) {{
      var h = Math.max(groupsA[i].offsetHeight, groupsB[i].offsetHeight);
      groupsA[i].style.minHeight = h + 'px';
      groupsB[i].style.minHeight = h + 'px';
    }}
  }}
  if (window.matchMedia('(min-width: 981px)').matches) {{
    align();
    window.addEventListener('resize', align);
  }}
}})();
</script>
</body>
</html>
"""


def render_html(data, history):
    generated_at = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    part1 = render_part("전일 마감 요약", 1, "06:00", data.get("yesterday"))
    part2 = render_part("금일 전망 · 주목 이슈", 2, "08:00", data.get("today"))
    trend = svg_trend_chart(history)
    html = HTML_SHELL.format(
        generated_at=generated_at, part_yesterday=part1, part_today=part2, trend_chart=trend
    )
    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    if os.path.isdir(os.path.join(SITE_DIR, ".git")):
        with open(SITE_INDEX_PATH, "w", encoding="utf-8") as f:
            f.write(html)


def publish_site(commit_message):
    """SITE_DIR가 git 저장소로 연결돼 있으면 변경분을 커밋/push한다. index.html은 항상 대상이고,
    데이터 JSON이 SITE_DIR 안에 있는 경우(GitHub Actions 배포 형태)엔 그것도 함께 커밋해서
    다음 실행이 이어받을 상태를 저장소 자체에 보존한다 (로컬 PC 배포에서는 데이터 JSON이
    SITE_DIR 밖에 있어서 자동으로 제외된다). 실패해도(오프라인, 인증 문제 등) 브리핑 생성
    자체는 항상 성공해야 하므로 예외를 삼키고 로그만 남긴다."""
    if not os.path.isdir(os.path.join(SITE_DIR, ".git")):
        return
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")  # never hang waiting on an interactive prompt
    add_paths = ["index.html"]
    for p in (DATA_PATH, HISTORY_PATH):
        if os.path.commonpath([os.path.abspath(p), SITE_DIR]) == SITE_DIR:
            add_paths.append(os.path.relpath(p, SITE_DIR))
    try:
        subprocess.run(
            ["git", "add", *add_paths], cwd=SITE_DIR, check=True, capture_output=True, text=True,
            env=env, timeout=30,
        )
        commit = subprocess.run(
            ["git", "commit", "-m", commit_message], cwd=SITE_DIR, capture_output=True, text=True,
            env=env, timeout=30,
        )
        if commit.returncode != 0:
            if "nothing to commit" in (commit.stdout + commit.stderr):
                print("  [git] 변경 사항 없음, publish 생략")
            else:
                print(f"  [git] commit 실패: {(commit.stdout + commit.stderr).strip()}")
            return
        push = subprocess.run(
            ["git", "push", "origin", "master"], cwd=SITE_DIR, capture_output=True, text=True,
            env=env, timeout=30,
        )
        if push.returncode != 0:
            print(f"  [git] push 실패: {(push.stdout + push.stderr).strip()}")
        else:
            print("  [git] GitHub Pages에 publish 완료")
    except Exception as e:
        print(f"  [git] publish 오류: {type(e).__name__}: {e}")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "today"
    if mode not in ("yesterday", "today", "usopen"):
        mode = "today"

    if mode == "usopen":
        # 미국장 개장 30분 전에 도는 트리거. 국내 쪽(kr/stocks)은 이미 08:30에 끝났으니 건드리지
        # 않고, "오늘" doc의 us/us_stocks만 패치한다: 해외 지수·핵심종목 실시간 시세를 무료 API로
        # 다시 받고, 전일 종가 + 개장 전 뉴스로 다우/S&P500/나스닥 오늘 방향까지 새로 예측한다.
        api_key = load_gemini_key()
        us_items = get_us_indices()
        us_stock_items = get_us_stock_quotes(US_STOCK_LIST)
        us_stock_news, us_stock_news_sources = get_stock_news(us_stock_items, api_key)
        us_outlook = predict_us_outlook(us_items, fetch_us_preopen_heads(), api_key)
        us_stock_outlook = predict_us_stock_outlook(us_stock_items, us_stock_news, api_key)

        data = load_json(DATA_PATH)
        today_doc = data.get("today")
        if today_doc:
            today_doc["us"] = {"items": us_items, "outlook": us_outlook, "reasons": None, "link": None}
            today_doc["us_stocks"] = {"items": us_stock_items, "news": us_stock_news, "outlook": us_stock_outlook}
            for s in us_stock_news_sources:
                if s not in today_doc.setdefault("sources", []):
                    today_doc["sources"].append(s)
            today_doc["updated_at"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
            data["today"] = today_doc
            save_json(DATA_PATH, data)

        history = load_json(HISTORY_PATH)
        render_html(data, history)
        publish_site(f"US pre-open update {datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')}")
        status = "AI 예측 생성" if us_outlook else "실시간 데이터만 갱신 (AI 키 없음/실패)"
        print(f"[usopen] {status} -> {HTML_PATH}")
        return

    api_key = load_gemini_key()
    doc, kr_raw = build_doc(mode, api_key)
    data = load_json(DATA_PATH)
    data[mode] = doc
    save_json(DATA_PATH, data)
    update_history(kr_raw, outlook=doc["kr"].get("outlook"))
    history = load_json(HISTORY_PATH)
    render_html(data, history)
    publish_site(f"Update briefing ({mode}) {datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')}")

    if "--no-open" not in sys.argv:
        try:
            os.startfile(HTML_PATH)
        except Exception:
            pass

    tag = "AI 요약" if doc.get("ai_summary") else "규칙기반(폴백)"
    print(f"[{mode}] briefing generated ({tag}) -> {HTML_PATH}")


if __name__ == "__main__":
    main()
