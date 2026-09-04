#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
valuebot.py  v1.0
애널리스트 보고서 -> 목표주가 산정 근거/계산식 역산 + 파이썬 검산 봇

설계 원칙
  1) LLM은 "추출"만 시키고, "산수"는 파이썬이 한다.
     (LLM 곱셈은 신뢰 불가 -> EPS x Multiple 검산은 코드로)
  2) 상주 롱폴링 루프 (cron 의존 X)
  3) Gemini 모델 런타임 디스커버리 + 폴백 체인
  4) 429 fail-fast + offset 선진행 (같은 메시지 무한 재시도 방지)

ENV
  VALUEBOT_TG_TOKEN    (필수) BotFather 토큰
  VALUEBOT_GEMINI_KEY  (필수) 이 봇 전용 Gemini 키
  VALUEBOT_ALLOWED     (선택) 허용 chat_id 콤마구분. 미설정 시 전체 허용
  VALUEBOT_MODEL       (선택) 모델 강제 지정
  VALUEBOT_MAX_RUNTIME (선택) 초. 기본 19800(5.5h)
"""

import os
import re
import sys
import json
import time
import base64
import traceback
import requests

# ----------------------------------------------------------------------------
# 설정
# ----------------------------------------------------------------------------
TG_TOKEN = os.environ.get("VALUEBOT_TG_TOKEN", "").strip()
GEMINI_KEY = os.environ.get("VALUEBOT_GEMINI_KEY", "").strip()
ALLOWED = {x.strip() for x in os.environ.get("VALUEBOT_ALLOWED", "").split(",") if x.strip()}
FORCED_MODEL = os.environ.get("VALUEBOT_MODEL", "").strip()
MAX_RUNTIME = int(os.environ.get("VALUEBOT_MAX_RUNTIME", "19800"))

TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"
TG_FILE = f"https://api.telegram.org/file/bot{TG_TOKEN}"
GEM_BASE = "https://generativelanguage.googleapis.com/v1beta"

INLINE_LIMIT = 14 * 1024 * 1024      # 이 이하는 inline base64, 초과는 File API
POLL_TIMEOUT = 50
START_TS = time.time()

MODEL_PREF = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.0-flash-001",
    "gemini-1.5-pro",
]
MODEL_BLOCK = re.compile(
    r"(tts|live|embedding|embed|aqa|transcribe|image|imagen|veo|vision-only|thinking-exp)",
    re.I,
)

STATE = {"model": None, "offset": 0}


def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


# ----------------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------------
def tg(method, **params):
    try:
        r = requests.post(f"{TG_API}/{method}", json=params, timeout=POLL_TIMEOUT + 15)
        return r.json()
    except Exception as e:
        log("TG ERR", method, e)
        return {"ok": False}


def send(chat_id, text, reply_to=None):
    """4096 제한 대응 분할 전송. HTML 파싱 실패 시 평문 폴백."""
    chunks, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > 3800:
            chunks.append(buf)
            buf = line
        else:
            buf = buf + "\n" + line if buf else line
    if buf:
        chunks.append(buf)

    for i, c in enumerate(chunks):
        p = {"chat_id": chat_id, "text": c, "disable_web_page_preview": True}
        if i == 0 and reply_to:
            p["reply_to_message_id"] = reply_to
        p["parse_mode"] = "HTML"
        res = tg("sendMessage", **p)
        if not res.get("ok"):
            p.pop("parse_mode", None)
            p["text"] = re.sub(r"<[^>]+>", "", c)
            tg("sendMessage", **p)
        time.sleep(0.3)


def download_tg_file(file_id):
    info = tg("getFile", file_id=file_id)
    if not info.get("ok"):
        return None, None
    path = info["result"]["file_path"]
    r = requests.get(f"{TG_FILE}/{path}", timeout=180)
    r.raise_for_status()
    return r.content, os.path.basename(path)


# ----------------------------------------------------------------------------
# Gemini
# ----------------------------------------------------------------------------
class QuotaExhausted(Exception):
    pass


def discover_model():
    if FORCED_MODEL:
        return FORCED_MODEL
    try:
        r = requests.get(f"{GEM_BASE}/models?key={GEMINI_KEY}&pageSize=200", timeout=30)
        data = r.json()
        avail = []
        for m in data.get("models", []):
            name = m.get("name", "").replace("models/", "")
            if "generateContent" not in m.get("supportedGenerationMethods", []):
                continue
            if MODEL_BLOCK.search(name):
                continue
            avail.append(name)
        for pref in MODEL_PREF:
            for a in avail:
                if a == pref:
                    return a
        for pref in MODEL_PREF:                      # 버전 접미사 허용
            for a in avail:
                if a.startswith(pref):
                    return a
        stable = [a for a in avail if "exp" not in a and "preview" not in a]
        if stable:
            return sorted(stable)[-1]
        if avail:
            return avail[0]
    except Exception as e:
        log("model discovery failed:", e)
    return "gemini-2.0-flash"


def gemini_generate(parts, model=None, temperature=0.1, max_tokens=8192):
    model = model or STATE["model"]
    url = f"{GEM_BASE}/models/{model}:generateContent?key={GEMINI_KEY}"
    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
            "responseMimeType": "application/json",
        },
    }
    last = None
    for attempt in range(3):
        r = requests.post(url, json=body, timeout=300)
        if r.status_code == 429:
            raise QuotaExhausted(r.text[:300])
        if r.status_code == 200:
            j = r.json()
            try:
                cand = j["candidates"][0]
                return "".join(p.get("text", "") for p in cand["content"]["parts"])
            except Exception:
                raise RuntimeError(f"unexpected response: {json.dumps(j)[:400]}")
        last = f"{r.status_code} {r.text[:300]}"
        if r.status_code in (500, 503):
            time.sleep(4 * (attempt + 1))
            continue
        break
    raise RuntimeError(last or "gemini failed")


def gemini_upload(data: bytes, mime: str, display_name: str) -> str:
    """File API 리주머블 업로드 -> file_uri 반환"""
    start = requests.post(
        f"https://generativelanguage.googleapis.com/upload/v1beta/files?key={GEMINI_KEY}",
        headers={
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(len(data)),
            "X-Goog-Upload-Header-Content-Type": mime,
            "Content-Type": "application/json",
        },
        json={"file": {"display_name": display_name}},
        timeout=60,
    )
    upload_url = start.headers.get("X-Goog-Upload-URL")
    if not upload_url:
        raise RuntimeError(f"upload start failed: {start.text[:300]}")

    up = requests.post(
        upload_url,
        headers={
            "Content-Length": str(len(data)),
            "X-Goog-Upload-Offset": "0",
            "X-Goog-Upload-Command": "upload, finalize",
        },
        data=data,
        timeout=600,
    )
    info = up.json().get("file", {})
    uri, name = info.get("uri"), info.get("name")
    if not uri:
        raise RuntimeError(f"upload failed: {up.text[:300]}")

    for _ in range(30):                              # ACTIVE 대기
        st = requests.get(
            f"https://generativelanguage.googleapis.com/v1beta/{name}?key={GEMINI_KEY}",
            timeout=30,
        ).json()
        if st.get("state") == "ACTIVE":
            return uri
        if st.get("state") == "FAILED":
            raise RuntimeError("file processing failed")
        time.sleep(2)
    return uri


# ----------------------------------------------------------------------------
# 추출 프롬프트  (LLM은 숫자를 '읽기'만 한다)
# ----------------------------------------------------------------------------
EXTRACT_PROMPT = """당신은 증권사 리서치 보고서의 밸류에이션 섹션만 정밀하게 해부하는 애널리스트다.
첨부된 보고서에서 '목표주가가 어떻게 계산되었는가'에만 집중해 아래 JSON 스키마로 추출하라.

절대 규칙:
- 보고서에 명시되지 않은 숫자는 절대 지어내지 말고 null로 둔다.
- 계산(곱셈/합산)은 하지 마라. 보고서에 적힌 값을 그대로 옮기기만 하라.
- 숫자는 단위 없는 순수 숫자로. 배수는 x 없이 (예: 12.5). 금액은 원 단위 정수 (예: 100000).
- 애매하거나 유추한 값은 confidence를 "low"로 표기하고 not_disclosed에 사유를 남긴다.
- 모든 서술형 문자열은 한국어로.

JSON 스키마:
{
  "stock": {"name": str|null, "ticker": str|null, "current_price": num|null, "currency": str|null},
  "report": {"broker": str|null, "analyst": str|null, "date": str|null,
             "rating": str|null, "target_price": num|null, "prev_target_price": num|null},
  "method": {"primary": "PER"|"PBR"|"SOTP"|"DCF"|"EV/EBITDA"|"기타"|null,
             "description": str|null},
  "per": {"eps": num|null, "eps_year": str|null,
          "eps_basis": str|null,
          "multiple": num|null, "multiple_rationale": str|null,
          "peer_set": str|null, "band_position": str|null,
          "premium_discount": str|null},
  "pbr": {"bps": num|null, "bps_year": str|null, "multiple": num|null,
          "roe": num|null, "coe": num|null, "growth": num|null, "rationale": str|null},
  "ev_ebitda": {"ebitda": num|null, "ebitda_year": str|null, "multiple": num|null,
                "net_debt": num|null, "shares": num|null, "rationale": str|null},
  "sotp": [{"segment": str, "method": str|null, "metric_name": str|null,
            "metric_value": num|null, "multiple": num|null,
            "value": num|null, "note": str|null}],
  "sotp_adjust": {"net_debt": num|null, "holdco_discount": num|null,
                  "shares": num|null, "note": str|null},
  "dcf": {"wacc": num|null, "terminal_growth": num|null, "horizon": str|null,
          "ev": num|null, "net_debt": num|null, "equity_value": num|null,
          "shares": num|null},
  "earnings_drivers": [str],
  "key_assumptions": [str],
  "not_disclosed": [str],
  "analyst_flags": [str],
  "confidence": "high"|"medium"|"low"
}

필드 보충 설명:
- eps_basis: "2027E 지배주주 EPS", "12개월 선행 EPS" 등 EPS의 정의를 그대로 옮긴다.
- band_position: 과거 밴드 대비 위치 서술 (예: "2019~2024 평균 10.2배 대비 상단").
- earnings_drivers: 목표주가의 전제가 되는 실적 가정 (출하량, ASP, 가동률, 환율 등).
- key_assumptions: 밸류에이션이 성립하기 위한 핵심 전제.
- not_disclosed: 보고서가 밝히지 않은 항목 (예: "적용 배수의 산출 근거 미기재").
- analyst_flags: 검증이 필요한 공격적 가정이나 논리적 비약. 냉정하게 지적하라.

JSON만 출력하라. 다른 텍스트, 마크다운 코드펜스 금지."""


# ----------------------------------------------------------------------------
# 파이썬 검산 로직
# ----------------------------------------------------------------------------
def g(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default


def num(v):
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = re.sub(r"[^\d.\-]", "", v)
        try:
            return float(s)
        except Exception:
            return None
    return None


def fmt(v, unit=""):
    if v is None:
        return "n/a"
    if abs(v) >= 1000:
        return f"{v:,.0f}{unit}"
    if abs(v) >= 10:
        return f"{v:,.1f}{unit}"
    return f"{v:,.2f}{unit}"


SCALES = [(1e2, "백"), (1e3, "천"), (1e4, "만"), (1e6, "백만"), (1e8, "억"), (1e12, "조")]


def reconcile_unit(calc, tp):
    """국내 보고서는 사업가치를 억원/백만원, 목표주가를 원으로 쓴다.
    단위 배수만 맞추면 일치하는 경우를 자동 탐지."""
    for s, label in SCALES:
        for cand, desc in ((calc * s, f"복원값 × {label}"), (calc / s, f"복원값 ÷ {label}")):
            if tp and abs((cand - tp) / tp) <= 0.02:
                return cand, desc
    return None, None


def verify(data):
    """산식 복원 + 검산. (라인 리스트, 검산결과 dict) 반환"""
    out = []
    tp = num(g(data, "report", "target_price"))
    method = (g(data, "method", "primary") or "").upper()
    result = {"ok": None, "diff": None}

    def check(calc, label):
        nonlocal result
        out.append(f"  복원값 : {fmt(calc)}")
        out.append(f"  보고서 : {fmt(tp)}")
        if not (tp and calc):
            return result
        diff = (calc - tp) / tp * 100
        result = {"ok": abs(diff) <= 2.0, "diff": diff}
        mark = "✅ 일치" if abs(diff) <= 2.0 else ("🟡 근사" if abs(diff) <= 5 else "🔴 불일치")
        out.append(f"  검산   : {mark} (괴리 {diff:+.1f}%)")
        if abs(diff) > 5:
            fixed, desc = reconcile_unit(calc, tp)
            if fixed:
                out.append(f"  🔧 단위 환산 시 일치: {desc} = {fmt(fixed)}")
                out.append("     → 산식 논리는 정합. 보고서의 금액 단위(억/백만원) 차이일 뿐.")
                result = {"ok": True, "diff": 0.0, "unit_fixed": desc}
            else:
                out.append(f"  ⚠️ 반올림/우선주·자사주 조정/미기재 변수 개입 가능. {label} 재확인 필요.")
        return result

    # --- PER ---
    eps, mult = num(g(data, "per", "eps")), num(g(data, "per", "multiple"))
    if eps and mult:
        out.append("〔산식 복원 · PER〕")
        out.append(f"  목표주가 = EPS {fmt(eps)}원 × {fmt(mult)}배")
        check(eps * mult, "EPS 기준연도")

    # --- PBR ---
    bps, pmult = num(g(data, "pbr", "bps")), num(g(data, "pbr", "multiple"))
    if bps and pmult:
        out.append("〔산식 복원 · PBR〕")
        out.append(f"  목표주가 = BPS {fmt(bps)}원 × {fmt(pmult)}배")
        roe, coe, gr = num(g(data, "pbr", "roe")), num(g(data, "pbr", "coe")), num(g(data, "pbr", "growth"))
        if roe and coe and coe != gr:
            implied = (roe - (gr or 0)) / (coe - (gr or 0))
            out.append(f"  이론 PBR = (ROE {roe}% − g {gr or 0}%) / (COE {coe}% − g {gr or 0}%) = {implied:.2f}배")
            gap = pmult - implied
            out.append(f"  적용배수 {fmt(pmult)}배 vs 이론 {implied:.2f}배 → {'프리미엄' if gap > 0 else '디스카운트'} {abs(gap):.2f}배")
        check(bps * pmult, "BPS 기준연도")

    # --- EV/EBITDA ---
    ebitda = num(g(data, "ev_ebitda", "ebitda"))
    emult = num(g(data, "ev_ebitda", "multiple"))
    endebt = num(g(data, "ev_ebitda", "net_debt"))
    esh = num(g(data, "ev_ebitda", "shares"))
    if ebitda and emult and esh:
        ev = ebitda * emult
        eq = ev - (endebt or 0)
        out.append("〔산식 복원 · EV/EBITDA〕")
        out.append(f"  EV = EBITDA {fmt(ebitda)} × {fmt(emult)}배 = {fmt(ev)}")
        out.append(f"  자기자본 = EV − 순차입금 {fmt(endebt or 0)} = {fmt(eq)}")
        out.append(f"  주당 = ÷ 주식수 {fmt(esh)}")
        check(eq / esh, "순차입금·주식수 기준")

    # --- SOTP ---
    segs = data.get("sotp") or []
    if segs:
        out.append("〔산식 복원 · SOTP〕")
        total = 0.0
        for s in segs:
            v = num(s.get("value"))
            mv, mu = num(s.get("metric_value")), num(s.get("multiple"))
            if v is None and mv and mu:
                v = mv * mu
            if v:
                total += v
            line = f"  · {s.get('segment','?')}"
            if mv and mu:
                line += f" = {fmt(mv)} × {fmt(mu)}배"
            line += f" → {fmt(v)}"
            out.append(line)
        nd = num(g(data, "sotp_adjust", "net_debt"))
        disc = num(g(data, "sotp_adjust", "holdco_discount"))
        sh = num(g(data, "sotp_adjust", "shares"))
        out.append(f"  합계 = {fmt(total)}")
        eq = total - (nd or 0)
        if nd:
            out.append(f"  − 순차입금 {fmt(nd)} → {fmt(eq)}")
        if disc:
            eq *= (1 - disc / 100.0)
            out.append(f"  − 지주 할인 {disc}% → {fmt(eq)}")
        if sh:
            out.append(f"  ÷ 주식수 {fmt(sh)}")
            check(eq / sh, "세그먼트 가치 합산")

    # --- DCF ---
    dev, dnd = num(g(data, "dcf", "ev")), num(g(data, "dcf", "net_debt"))
    deq, dsh = num(g(data, "dcf", "equity_value")), num(g(data, "dcf", "shares"))
    if dsh and (deq or dev):
        out.append("〔산식 복원 · DCF〕")
        eq = deq if deq else dev - (dnd or 0)
        if not deq:
            out.append(f"  자기자본 = EV {fmt(dev)} − 순차입금 {fmt(dnd or 0)} = {fmt(eq)}")
        w, tg_ = num(g(data, "dcf", "wacc")), num(g(data, "dcf", "terminal_growth"))
        if w is not None and tg_ is not None:
            out.append(f"  WACC {w}% / 영구성장률 {tg_}% (스프레드 {w - tg_:.1f}%p)")
            if w - tg_ < 3:
                out.append("  ⚠️ WACC−g 스프레드 3%p 미만 → 터미널밸류 민감도 극단적. 가정 공격적.")
        check(eq / dsh, "주식수 기준")

    if not out:
        out.append("〔산식 복원〕")
        out.append("  ⚠️ 보고서에서 목표주가 산식을 구성할 변수를 찾지 못했습니다.")
        out.append(f"  기재된 방법론: {method or '미확인'}")

    return out, result


def sensitivity(data):
    """PER/PBR 기반일 때만 민감도 그리드 생성"""
    eps, mult = num(g(data, "per", "eps")), num(g(data, "per", "multiple"))
    label = "EPS"
    if not (eps and mult):
        eps, mult = num(g(data, "pbr", "bps")), num(g(data, "pbr", "multiple"))
        label = "BPS"
    if not (eps and mult):
        return []

    # 배수 크기에 비례한 스텝 (PBR 1.0배에 ±1 스텝은 무의미)
    if mult < 3:
        step = 0.1
    elif mult < 10:
        step = 0.5
    else:
        step = 1.0
    steps = [-2 * step, -step, 0, step, 2 * step]
    rows = [f"〔민감도〕 배수 ±{step * 2:g}, " + label + " ±10%"]
    header = f"  {'배수':>6} │ {label+'-10%':>10} {'기준':>10} {label+'+10%':>10}"
    rows.append(header)
    rows.append("  " + "─" * 42)
    for s in steps:
        m = mult + s
        if m <= 0:
            continue
        cells = [eps * 0.9 * m, eps * m, eps * 1.1 * m]
        star = "◀" if abs(s) < 1e-9 else " "
        mtxt = f"{m:.2f}" if step < 0.5 else f"{m:.1f}"
        rows.append(f"  {mtxt:>5}x │ " + " ".join(f"{c:>10,.0f}" for c in cells) + star)
    return rows


def render(data, filename):
    L = []
    name = g(data, "stock", "name") or "종목 미상"
    tick = g(data, "stock", "ticker")
    broker = g(data, "report", "broker") or "출처 미상"
    date = g(data, "report", "date") or ""
    rating = g(data, "report", "rating") or "-"
    tp = num(g(data, "report", "target_price"))
    prev = num(g(data, "report", "prev_target_price"))
    cur = num(g(data, "stock", "current_price"))

    head = f"📊 <b>{name}</b>"
    if tick:
        head += f" ({tick})"
    L.append(head)
    L.append(f"{broker} · {date} · <b>{rating}</b>")

    tpline = f"목표주가 {fmt(tp)}"
    if prev and tp:
        chg = (tp - prev) / prev * 100
        tpline += f" (직전 {fmt(prev)}, {chg:+.1f}%)"
    L.append(tpline)
    if cur and tp:
        up = (tp - cur) / cur * 100
        L.append(f"현재가 {fmt(cur)} → 상승여력 {up:+.1f}%")
        if up > 200 or up < -80:
            L.append("⚠️ 상승여력이 비정상적입니다. 현재가/목표주가 단위 추출 오류 가능 — 원문 대조 필요.")
    L.append("")

    L.append(f"<b>1. 방법론</b>: {g(data,'method','primary') or '미확인'}")
    desc = g(data, "method", "description")
    if desc:
        L.append(f"   {desc}")
    L.append("")

    L.append("<b>2. 계산식 역산 및 검산</b>")
    vlines, _ = verify(data)
    L.append("<pre>" + "\n".join(vlines) + "</pre>")

    # 투입 변수
    L.append("<b>3. 투입 변수</b>")
    rows = []
    if num(g(data, "per", "eps")):
        rows.append(f"  EPS      {fmt(num(g(data,'per','eps')))}원  [{g(data,'per','eps_year') or '?'}]")
        basis = g(data, "per", "eps_basis")
        if basis:
            rows.append(f"    └ 정의: {basis}")
        rows.append(f"  적용배수  {fmt(num(g(data,'per','multiple')))}배")
    if num(g(data, "pbr", "bps")):
        rows.append(f"  BPS      {fmt(num(g(data,'pbr','bps')))}원  [{g(data,'pbr','bps_year') or '?'}]")
        rows.append(f"  적용배수  {fmt(num(g(data,'pbr','multiple')))}배")
        for k, lab in (("roe", "ROE"), ("coe", "COE"), ("growth", "g")):
            v = num(g(data, "pbr", k))
            if v is not None:
                rows.append(f"  {lab:<8} {v}%")
    if not rows:
        rows.append("  (개별 투입 변수 미기재)")
    L.append("<pre>" + "\n".join(rows) + "</pre>")

    # 배수 근거
    L.append("<b>4. 배수 산정 근거</b>")
    got = False
    for path, lab in (
        (("per", "multiple_rationale"), "근거"),
        (("per", "band_position"), "밴드 위치"),
        (("per", "premium_discount"), "프리미엄/할인"),
        (("per", "peer_set"), "Peer"),
        (("pbr", "rationale"), "근거(PBR)"),
        (("ev_ebitda", "rationale"), "근거(EV/EBITDA)"),
    ):
        v = g(data, *path)
        if v:
            L.append(f"   • {lab}: {v}")
            got = True
    if not got:
        L.append("   • ⚠️ 배수 선택의 정량적 근거가 보고서에 제시되지 않음")
    L.append("")

    sens = sensitivity(data)
    if sens:
        L.append("<pre>" + "\n".join(sens) + "</pre>")

    def bullets(key, title, emoji=""):
        items = data.get(key) or []
        if items:
            L.append(f"<b>{emoji}{title}</b>")
            for it in items[:6]:
                L.append(f"   • {it}")
            L.append("")

    bullets("earnings_drivers", "5. 실적 가정 (목표주가의 전제)")
    bullets("key_assumptions", "6. 핵심 전제")
    bullets("not_disclosed", "7. 미기재 항목", "⚠️ ")
    bullets("analyst_flags", "8. 검증 포인트", "🔍 ")

    conf = data.get("confidence") or "?"
    L.append(f"<i>추출 신뢰도: {conf} · {filename} · {STATE['model']}</i>")
    return "\n".join(L)


# ----------------------------------------------------------------------------
# 처리
# ----------------------------------------------------------------------------
HELP = """<b>밸류에이션 역산 봇</b>

증권사 보고서를 넣으면 <b>목표주가가 어떤 산식으로 나왔는지</b> 역산하고,
그 곱셈이 실제로 맞는지 파이썬으로 검산해 드립니다.

<b>사용법</b>
 · PDF 보고서 파일 전송 → 자동 분석
 · 보고서 본문 텍스트 붙여넣기 (200자 이상) → 자동 분석

<b>출력</b>
 1 방법론 (PER/PBR/SOTP/DCF/EV·EBITDA)
 2 계산식 역산 + 검산 (✅일치 / 🔴불일치)
 3 투입 변수 (EPS·BPS·기준연도·정의)
 4 배수 산정 근거 (밴드·Peer·프리미엄)
 5 민감도 (배수 ±2x, EPS ±10%)
 6 미기재 항목 / 검증 포인트

<b>명령어</b>
 /help  도움말
 /model 현재 모델
 /id    내 chat_id"""


def analyze(chat_id, parts, filename, msg_id):
    tg("sendChatAction", chat_id=chat_id, action="typing")
    raw = gemini_generate([{"text": EXTRACT_PROMPT}] + parts)
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            raise RuntimeError("JSON 파싱 실패")
        data = json.loads(m.group(0))
    send(chat_id, render(data, filename), reply_to=msg_id)


def handle(msg):
    chat_id = str(msg["chat"]["id"])
    msg_id = msg.get("message_id")
    if ALLOWED and chat_id not in ALLOWED:
        return
    text = (msg.get("text") or msg.get("caption") or "").strip()

    if text.startswith("/start") or text.startswith("/help"):
        send(chat_id, HELP)
        return
    if text.startswith("/model"):
        send(chat_id, f"모델: <code>{STATE['model']}</code>")
        return
    if text.startswith("/id"):
        send(chat_id, f"chat_id: <code>{chat_id}</code>")
        return

    doc = msg.get("document")
    try:
        if doc:
            fname = doc.get("file_name", "report.pdf")
            mime = doc.get("mime_type") or "application/pdf"
            if not (fname.lower().endswith(".pdf") or "pdf" in mime):
                send(chat_id, "PDF 파일만 지원합니다. 텍스트는 그냥 붙여넣어 주세요.")
                return
            send(chat_id, f"📥 <code>{fname}</code> 분석 중…")
            data, _ = download_tg_file(doc["file_id"])
            if data is None:
                send(chat_id, "파일 다운로드 실패 (텔레그램 봇 다운로드 한도 20MB).")
                return
            if len(data) <= INLINE_LIMIT:
                parts = [{"inline_data": {"mime_type": "application/pdf",
                                          "data": base64.b64encode(data).decode()}}]
            else:
                uri = gemini_upload(data, "application/pdf", fname)
                parts = [{"file_data": {"mime_type": "application/pdf", "file_uri": uri}}]
            analyze(chat_id, parts, fname, msg_id)

        elif len(text) >= 200:
            send(chat_id, "📥 텍스트 분석 중…")
            analyze(chat_id, [{"text": "<보고서 본문>\n" + text}], "텍스트 입력", msg_id)

        elif text:
            send(chat_id, "보고서 PDF를 보내시거나 본문(200자 이상)을 붙여넣어 주세요. /help")

    except QuotaExhausted:
        send(chat_id, "🔴 Gemini 무료 쿼터 소진. 잠시 후 다시 시도해 주세요.\n(이 봇 전용 키를 쓰고 있는지 확인 권장)")
    except Exception as e:
        log("handle error:", traceback.format_exc())
        send(chat_id, f"❌ 처리 실패: <code>{str(e)[:300]}</code>")


# ----------------------------------------------------------------------------
def main():
    if not TG_TOKEN or not GEMINI_KEY:
        log("VALUEBOT_TG_TOKEN / VALUEBOT_GEMINI_KEY 미설정")
        sys.exit(1)

    STATE["model"] = discover_model()
    log("model:", STATE["model"])

    me = tg("getMe")
    log("bot:", me.get("result", {}).get("username"))

    # 백로그 스킵
    init = tg("getUpdates", timeout=0, offset=-1)
    if init.get("ok") and init.get("result"):
        STATE["offset"] = init["result"][-1]["update_id"] + 1
        log("skip backlog, offset =", STATE["offset"])

    while time.time() - START_TS < MAX_RUNTIME:
        try:
            r = tg("getUpdates", offset=STATE["offset"], timeout=POLL_TIMEOUT,
                   allowed_updates=["message"])
            if not r.get("ok"):
                time.sleep(5)
                continue
            for upd in r.get("result", []):
                STATE["offset"] = upd["update_id"] + 1     # 먼저 전진 (무한루프 방지)
                if "message" in upd:
                    handle(upd["message"])
        except requests.exceptions.ReadTimeout:
            continue
        except Exception:
            log("loop error:", traceback.format_exc())
            time.sleep(5)

    log("runtime limit reached, exiting cleanly")


if __name__ == "__main__":
    main()
