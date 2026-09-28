#!/usr/bin/env python3
"""
English Coach Bot — Discord 구동사 치트시트 & 아침 복습 카드

사용법:
  python scripts/send_cheat_sheet.py --mode prep     # 수업 전 치트시트 (19:10 KST)
  python scripts/send_cheat_sheet.py --mode review   # 다음 날 아침 복습 카드 (08:00 KST)

환경 변수:
  필수  DISCORD_WEBHOOK_URL
  필수  GEMINI_API_KEY 또는 GROQ_API_KEY (둘 다 있으면 Gemini 우선, 실패 시 Groq 폴백)
  선택  DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID  → 복습 시 채널의 튜터 피드백을 읽음
  선택  GEMINI_MODEL (기본 gemini-2.5-flash), GROQ_MODEL (기본 llama-3.3-70b-versatile)
  선택  CLASS_TIME (기본 19:20), DRY_RUN=1 (Discord 발송·히스토리 저장 없이 출력만)
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ───────────────────────── 설정 ─────────────────────────
KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
HISTORY_PATH = ROOT / "data" / "history.json"
HISTORY_KEEP = 60          # 보관할 수업 기록 수
AVOID_LOOKBACK = 20        # 최근 N회 수업의 표현은 재출제 금지
FEEDBACK_MAX_CHARS = 8000  # LLM에 넘길 피드백 최대 길이


def env(name: str, default: str = "") -> str:
    # GitHub Actions에서 미설정 secret은 빈 문자열로 들어오므로 `or default` 처리
    return (os.getenv(name) or default).strip()


GEMINI_API_KEY = env("GEMINI_API_KEY")
GEMINI_MODEL = env("GEMINI_MODEL", "gemini-2.5-flash")
GROQ_API_KEY = env("GROQ_API_KEY")
GROQ_MODEL = env("GROQ_MODEL", "llama-3.3-70b-versatile")
DISCORD_WEBHOOK_URL = env("DISCORD_WEBHOOK_URL")
DISCORD_BOT_TOKEN = env("DISCORD_BOT_TOKEN")
DISCORD_CHANNEL_ID = env("DISCORD_CHANNEL_ID")
CLASS_TIME = env("CLASS_TIME", "19:20")
DRY_RUN = env("DRY_RUN") == "1"

RETRYABLE = {429, 500, 502, 503, 504}
WEEKDAYS_KO = ["월", "화", "수", "목", "금", "토", "일"]
THEMES = [
    "work & meetings", "daily routine", "plans & schedules", "feelings & reactions",
    "problems & solutions", "socializing & small talk", "health & energy",
    "money & shopping", "learning & self-improvement", "commuting & travel",
]

# ───────────────────────── 프롬프트 ─────────────────────────
PREP_SYSTEM = """You are an expert English conversation coach for Korean speakers.
Generate 2 practical, conversational phrasal verbs for an upcoming 30-minute 1:1 online English conversation class.

Target level: B1 to B2 (Intermediate).
Criteria:
- Choose phrasal verbs that native speakers use frequently in daily life or work, but Korean learners usually replace with a single generic verb (e.g. 'call off' instead of 'cancel', 'put off' instead of 'postpone', 'wrap up' instead of 'finish').
- The learner understands them when reading or listening but rarely uses them when speaking.
- Avoid academic, archaic, or vulgar expressions.
- The two phrasal verbs must differ clearly in meaning.

Return ONLY a JSON object with exactly this shape:
{"verbs": [
  {"verb": "...", "meaning_ko": "...", "replaces": "...", "nuance_ko": "...", "example": "...", "my_line": "..."},
  {"verb": "...", "meaning_ko": "...", "replaces": "...", "nuance_ko": "...", "example": "...", "my_line": "..."}
]}

Field rules:
- verb: base form, lowercase (e.g. "wrap up").
- meaning_ko: short Korean meaning (e.g. "마무리하다").
- replaces: the generic single verb Korean learners use instead (e.g. "finish").
- nuance_ko: ONE Korean sentence on nuance or usage (casual vs formal, separable or not, when natives prefer it).
- example: one natural English sentence, 10-18 words.
- my_line: a ready-to-speak first-person sentence the learner can say in class about their own day or week, 8-18 words."""

REVIEW_SYSTEM = """You are an expert English conversation coach for Korean speakers.
Create a short morning review card for a B1-B2 learner, based on yesterday's 1:1 online English lesson.

Return ONLY a JSON object with exactly this shape:
{"focus_ko": "...",
 "read_aloud": [{"sentence": "...", "ko": "..."}, {"sentence": "...", "ko": "..."}],
 "quiz": {"question": "...", "answer": "...", "hint_ko": "..."}}

Rules:
- focus_ko: ONE Korean sentence summarizing today's review point.
- read_aloud: exactly 2 natural English sentences (8-18 words) to read aloud 3 times; ko is a natural Korean translation.
- If tutor feedback is provided, prioritize it: turn the learner's mistakes into corrected sentences and reuse useful expressions the tutor taught. Ignore greetings, links, and small talk.
- If no feedback is provided, build the sentences around the target phrasal verbs.
- quiz.question: one English sentence with exactly one blank written as "____" (four underscores), testing a target expression or a corrected mistake.
- quiz.answer: the exact words that fill the blank.
- quiz.hint_ko: a short Korean hint that does NOT reveal the answer."""


# ───────────────────────── 히스토리 ─────────────────────────
def load_history() -> list[dict]:
    try:
        data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_history(history: list[dict]) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(
        json.dumps(history[-HISTORY_KEEP:], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


# ───────────────────────── LLM ─────────────────────────
def post_with_retry(url: str, *, headers: dict, payload: dict, attempts: int = 3) -> dict:
    last = ""
    for i in range(attempts):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=90)
        except requests.RequestException as e:
            last = str(e)
            time.sleep(5 * (i + 1))
            continue
        if r.status_code in RETRYABLE:
            last = f"HTTP {r.status_code}: {r.text[:300]}"
            time.sleep(5 * (i + 1))
            continue
        if not r.ok:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:500]}")
        return r.json()
    raise RuntimeError(f"재시도 초과 — {last}")


def call_gemini(system: str, user: str) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.9},
    }
    data = post_with_retry(
        url,
        headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
        payload=payload,
    )
    try:
        parts = data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError):
        raise RuntimeError(f"Gemini 응답 형식 이상: {json.dumps(data)[:300]}")
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))


def call_groq(system: str, user: str) -> str:
    data = post_with_retry(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
        payload={
            "model": GROQ_MODEL,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.9,
            "response_format": {"type": "json_object"},
        },
    )
    return data["choices"][0]["message"]["content"]


def strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t.rsplit("```", 1)[0]
    return t.strip()


def generate_json(system: str, user: str, validate) -> dict:
    providers = []
    if GEMINI_API_KEY:
        providers.append(("Gemini", call_gemini))
    if GROQ_API_KEY:
        providers.append(("Groq", call_groq))
    if not providers:
        raise RuntimeError("GEMINI_API_KEY 또는 GROQ_API_KEY가 설정되지 않았습니다.")

    errors = []
    for name, fn in providers:
        for attempt in range(1, 3):
            try:
                obj = json.loads(strip_fences(fn(system, user)))
                validate(obj)
                print(f"[LLM] {name} 응답 OK (시도 {attempt})")
                return obj
            except Exception as e:  # 파싱·검증 실패 포함
                errors.append(f"{name}#{attempt}: {e}")
                print(f"[LLM] {name} 시도 {attempt} 실패: {e}", file=sys.stderr)
    raise RuntimeError("모든 LLM 호출 실패 — " + " | ".join(errors))


def _require_str(d: dict, keys: list[str], where: str) -> None:
    for k in keys:
        if not isinstance(d.get(k), str) or not d[k].strip():
            raise ValueError(f"{where}.{k} 누락")


def validate_prep(obj: dict) -> None:
    verbs = obj.get("verbs")
    if not isinstance(verbs, list) or len(verbs) < 2:
        raise ValueError("verbs 항목이 2개 미만")
    for i, v in enumerate(verbs[:2]):
        _require_str(v, ["verb", "meaning_ko", "replaces", "nuance_ko", "example", "my_line"], f"verbs[{i}]")
    if verbs[0]["verb"].lower().strip() == verbs[1]["verb"].lower().strip():
        raise ValueError("두 구동사가 동일")


def validate_review(obj: dict) -> None:
    _require_str(obj, ["focus_ko"], "root")
    ra = obj.get("read_aloud")
    if not isinstance(ra, list) or len(ra) < 2:
        raise ValueError("read_aloud 항목이 2개 미만")
    for i, s in enumerate(ra[:2]):
        _require_str(s, ["sentence", "ko"], f"read_aloud[{i}]")
    quiz = obj.get("quiz") or {}
    _require_str(quiz, ["question", "answer", "hint_ko"], "quiz")
    if "__" not in quiz["question"]:
        raise ValueError("quiz.question에 빈칸(____)이 없음")


# ───────────────────────── Discord ─────────────────────────
def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def send_discord(embed: dict) -> None:
    payload = {"username": "English Coach", "embeds": [embed]}
    if DRY_RUN:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL이 설정되지 않았습니다.")
    r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"Discord 발송 실패 HTTP {r.status_code}: {r.text[:300]}")
    print("[Discord] 발송 완료")


def notify_failure(mode: str, err: Exception) -> None:
    if DRY_RUN or not DISCORD_WEBHOOK_URL:
        return
    label = "수업 전 치트시트" if mode == "prep" else "아침 복습 카드"
    try:
        requests.post(
            DISCORD_WEBHOOK_URL,
            json={"username": "English Coach", "embeds": [{
                "title": f"⚠️ {label} 생성 실패",
                "description": clip(f"```{err}```", 1500) + "\nGitHub Actions 로그를 확인해 주세요.",
                "color": 0xED4245,
            }]},
            timeout=15,
        )
    except requests.RequestException:
        pass


def fetch_feedback(since: datetime) -> str:
    """봇 토큰이 있으면 `since` 이후 채널에 사람이 올린 메시지(+ .txt 첨부)를 모은다."""
    if not (DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID):
        print("[Feedback] 봇 미설정 → 전날 표현으로 복습")
        return ""
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
    try:
        r = requests.get(
            f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages",
            headers=headers, params={"limit": 50}, timeout=30,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"[Feedback] 메시지 조회 실패 → 전날 표현으로 복습: {e}", file=sys.stderr)
        return ""

    chunks: list[tuple[datetime, str]] = []
    for m in r.json():
        if m.get("webhook_id") or (m.get("author") or {}).get("bot"):
            continue
        ts = datetime.fromisoformat(m["timestamp"])
        if ts < since:
            continue
        parts = [(m.get("content") or "").strip()]
        # 2000자 넘는 붙여넣기는 Discord가 message.txt 첨부로 바꾸므로 함께 읽는다
        for att in m.get("attachments", []):
            if att.get("filename", "").endswith(".txt") and att.get("size", 0) < 200_000:
                try:
                    parts.append(requests.get(att["url"], timeout=30).text.strip())
                except requests.RequestException:
                    pass
        text = "\n".join(p for p in parts if p)
        if text:
            chunks.append((ts, text))

    chunks.sort(key=lambda x: x[0])
    joined = "\n---\n".join(t for _, t in chunks)
    print(f"[Feedback] 메시지 {len(chunks)}개, {len(joined)}자 수집")
    return joined[-FEEDBACK_MAX_CHARS:]


# ───────────────────────── Phase 1: 수업 전 ─────────────────────────
def build_prep_embed(verbs: list[dict], now_kst: datetime) -> dict:
    fields = []
    for i, v in enumerate(verbs, 1):
        value = (
            f"• **뉘앙스:** `{v['replaces']}` 대신 → {v['nuance_ko']}\n"
            f"• **예문:** \"{v['example']}\"\n"
            f"• **내 대사 치트키:** \"{v['my_line']}\""
        )
        fields.append({"name": clip(f"{i}. {v['verb']} ({v['meaning_ko']})", 256),
                       "value": clip(value, 1024), "inline": False})
    opener = (f"\"Before we jump into the article, I'm trying to use "
              f"'{verbs[0]['verb']}' and '{verbs[1]['verb']}' today!\"")
    fields.append({"name": "🗣️ 튜터에게 던질 첫 마디", "value": clip(opener, 1024), "inline": False})
    return {
        "title": f"🎯 [오늘의 {CLASS_TIME} 회화 치트키]",
        "color": 0x5865F2,
        "fields": fields,
        "footer": {"text": f"{now_kst:%Y-%m-%d} ({WEEKDAYS_KO[now_kst.weekday()]}) · 수업 후 피드백은 이 채널에 붙여넣기"},
        "timestamp": now_kst.astimezone(timezone.utc).isoformat(),
    }


def run_prep() -> None:
    history = load_history()
    recent = sorted({v["verb"].lower() for h in history[-AVOID_LOOKBACK:] for v in h.get("verbs", [])})
    now = datetime.now(timezone.utc)
    now_kst = now.astimezone(KST)

    user = (
        f"Today is {now_kst:%A, %Y-%m-%d}. Loose theme for variety: {random.choice(THEMES)}.\n"
        f"Do NOT use any of these recently used phrasal verbs: {', '.join(recent) or '(none)'}."
    )
    obj = generate_json(PREP_SYSTEM, user, validate_prep)
    verbs = [{k: v[k].strip() for k in ("verb", "meaning_ko", "replaces", "nuance_ko", "example", "my_line")}
             for v in obj["verbs"][:2]]
    for v in verbs:
        if v["verb"].lower() in recent:
            print(f"[WARN] 최근 출제된 표현 재등장: {v['verb']}", file=sys.stderr)

    send_discord(build_prep_embed(verbs, now_kst))

    if not DRY_RUN:
        history.append({"date": now_kst.date().isoformat(), "sent_at": now.isoformat(), "verbs": verbs})
        save_history(history)
        print(f"[History] 저장 완료 ({len(history)}건)")


# ───────────────────────── Phase 3: 아침 복습 ─────────────────────────
def build_review_embed(obj: dict, source: str, now_kst: datetime) -> dict:
    ra = obj["read_aloud"][:2]
    read_value = "\n".join(f"**{i}.** {s['sentence']}\n　↳ {s['ko']}" for i, s in enumerate(ra, 1))
    q = obj["quiz"]
    quiz_value = f"{q['question']}\n💡 힌트: {q['hint_ko']}\n✅ 정답: ||{q['answer']}||"
    return {
        "title": "🌅 [아침 복습 카드]",
        "description": clip(f"{obj['focus_ko']}\n-# {source}", 4096),
        "color": 0xFEE75C,
        "fields": [
            {"name": "🔁 소리 내어 3번 읽기", "value": clip(read_value, 1024), "inline": False},
            {"name": "✏️ 빈칸 채우기", "value": clip(quiz_value, 1024), "inline": False},
        ],
        "footer": {"text": f"{now_kst:%Y-%m-%d} ({WEEKDAYS_KO[now_kst.weekday()]})"},
        "timestamp": now_kst.astimezone(timezone.utc).isoformat(),
    }


def run_review() -> None:
    history = load_history()
    now = datetime.now(timezone.utc)
    now_kst = now.astimezone(KST)
    last = history[-1] if history else None

    since = datetime.fromisoformat(last["sent_at"]) if last else now - timedelta(hours=16)
    feedback = fetch_feedback(since)

    targets = list(last["verbs"]) if last else []
    older = [v for h in history[-30:-1] for v in h.get("verbs", [])]
    if older:
        targets.append(random.choice(older))  # 간격 반복용으로 예전 표현 1개 추가
    target_txt = "\n".join(f"- {v['verb']} ({v['meaning_ko']}): {v['example']}" for v in targets) \
        or "- (none — choose 2 useful everyday phrasal verbs yourself)"

    if feedback:
        source = "어제 튜터 피드백 기반"
        user = (f"Target phrasal verbs from yesterday:\n{target_txt}\n\n"
                f"Tutor feedback / chat log from yesterday's lesson:\n<<<\n{feedback}\n>>>")
    else:
        source = "어제 치트키 표현 복습 (피드백 없음)"
        user = f"No tutor feedback was provided. Target phrasal verbs:\n{target_txt}"

    obj = generate_json(REVIEW_SYSTEM, user, validate_review)
    send_discord(build_review_embed(obj, source, now_kst))


# ───────────────────────── main ─────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["prep", "review"], required=True)
    mode = parser.parse_args().mode
    try:
        run_prep() if mode == "prep" else run_review()
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        notify_failure(mode, e)
        sys.exit(1)


if __name__ == "__main__":
    main()
