#!/usr/bin/env python3
"""
English Coach Bot — Discord 구동사 치트시트 & 아침 복습 카드

사용법:
  --mode prep      수업 전 치트시트 (18:40 KST)
  --mode review    아침 복습 카드
  --mode articles  오늘의 아티클 3선 (기사 링크 + 주제 + vocab 7 + 스몰톡 질문 5)
  --mode morning   review + articles (각각 별도 메시지, 08:00 KST)
  --mode sources   기사 소스 점검만 (발송 없음)

환경 변수:
  필수  DISCORD_WEBHOOK_URL
  필수  GEMINI_API_KEY 또는 GROQ_API_KEY (둘 다 있으면 Gemini 우선, 실패 시 Groq 폴백)
  선택  DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID  → 복습 시 채널의 튜터 피드백을 읽음
  선택  GEMINI_MODEL (기본 gemini-3.5-flash-lite), GROQ_MODEL (기본 openai/gpt-oss-120b)
  선택  LLM_ORDER (기본 gemini,groq — 먼저 시도할 순서)
  선택  ARTICLE_WEIGHTS (기본 engoo:0.5,sedaily:0.3,ted:0.2), ENGOO_LIST_URL
  선택  CLASS_TIME (기본 19:20), DRY_RUN=1 (Discord 발송·히스토리 저장 없이 출력만)
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

import articles as art

# ───────────────────────── 설정 ─────────────────────────
KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
HISTORY_PATH = ROOT / "data" / "history.json"
ARTICLES_HISTORY_PATH = ROOT / "data" / "articles_history.json"
HISTORY_KEEP = 60          # 보관할 수업 기록 수
AVOID_LOOKBACK = 20        # 최근 N회 수업의 표현은 재출제 금지
FEEDBACK_MAX_CHARS = 8000  # LLM에 넘길 피드백 최대 길이


def env(name: str, default: str = "") -> str:
    # GitHub Actions에서 미설정 secret은 빈 문자열로 들어오므로 `or default` 처리
    return (os.getenv(name) or default).strip()


GEMINI_API_KEY = env("GEMINI_API_KEY")
GEMINI_MODEL = env("GEMINI_MODEL", "gemini-3.5-flash-lite")
GROQ_API_KEY = env("GROQ_API_KEY")
GROQ_MODEL = env("GROQ_MODEL", "openai/gpt-oss-120b")
LLM_ORDER = [x.strip().lower() for x in env("LLM_ORDER", "gemini,groq").split(",") if x.strip()]
DISCORD_WEBHOOK_URL = env("DISCORD_WEBHOOK_URL")
DISCORD_BOT_TOKEN = env("DISCORD_BOT_TOKEN")
DISCORD_CHANNEL_ID = env("DISCORD_CHANNEL_ID")
CLASS_TIME = env("CLASS_TIME", "19:20")
DRY_RUN = env("DRY_RUN") == "1"
ARTICLE_WEIGHTS = art.parse_weights(env("ARTICLE_WEIGHTS", "engoo:0.5,sedaily:0.3,ted:0.2"))

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
def load_json_list(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_json_list(path: Path, items: list[dict], keep: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items[-keep:], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_history() -> list[dict]:
    return load_json_list(HISTORY_PATH)


def save_history(history: list[dict]) -> None:
    save_json_list(HISTORY_PATH, history, HISTORY_KEEP)


# ───────────────────────── LLM ─────────────────────────
def post_with_retry(url: str, *, headers: dict, payload: dict, attempts: int = 2) -> dict:
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
            time.sleep(8 * (i + 1))
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
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.9,
                             "maxOutputTokens": 8192},
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
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.9,
        "response_format": {"type": "json_object"},
        "max_completion_tokens": 8192,
    }
    if "gpt-oss" in GROQ_MODEL:
        payload["reasoning_effort"] = "low"  # 단순 생성 작업이라 추론은 짧게
    data = post_with_retry(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
        payload=payload,
    )
    return data["choices"][0]["message"]["content"]


def strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t.rsplit("```", 1)[0]
    return t.strip()


def generate_json(system: str, user: str, validate) -> dict:
    available = {"gemini": ("Gemini", call_gemini) if GEMINI_API_KEY else None,
                 "groq": ("Groq", call_groq) if GROQ_API_KEY else None}
    order = LLM_ORDER + [k for k in available if k not in LLM_ORDER]
    providers = [available[k] for k in order if available.get(k)]
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
    label = {"prep": "수업 전 치트시트", "review": "아침 복습 카드", "articles": "오늘의 아티클"}.get(mode, mode)
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


# ───────────────────────── 오늘의 아티클 ─────────────────────────
ARTICLE_SYSTEM = """You are an English conversation coach for a Korean B1-B2 learner who enjoys current, youth-friendly topics.
For EACH article below, create study material based on its content.

Return ONLY a JSON object with exactly this shape:
{"articles": [
  {"id": 1, "topic_ko": "...", "summary_ko": "...",
   "vocab": [{"word": "...", "meaning_ko": "...", "example": "..."}],
   "questions": ["...", "...", "...", "...", "..."]}
]}

Rules:
- One object per article, with the same id and in the same order as given.
- topic_ko: the topic as a short Korean keyword phrase (about 15 characters, e.g. "AI와 일자리의 미래").
- summary_ko: 2 Korean sentences on what the article or talk is about. If only a title is available, describe the likely topic carefully and do NOT invent facts, names, or numbers.
- vocab: exactly 7 useful words or expressions (B1-C1) that appear in or clearly fit the article. Prefer collocations, phrasal verbs, and words usable in conversation. meaning_ko is short; example is one natural English sentence (at most 12 words) related to the topic.
- questions: exactly 5 open-ended small-talk questions in English (8-20 words each) for discussing the article with a tutor, going from easy and personal to opinion-based."""


def validate_articles(n: int):
    def _v(obj: dict) -> None:
        items = obj.get("articles")
        if not isinstance(items, list) or len(items) < n:
            raise ValueError(f"articles 항목이 {n}개 미만")
        for i, a in enumerate(items[:n]):
            _require_str(a, ["topic_ko", "summary_ko"], f"articles[{i}]")
            voc = a.get("vocab")
            if not isinstance(voc, list) or len(voc) < 7:
                raise ValueError(f"articles[{i}].vocab 7개 미만")
            for j, v in enumerate(voc[:7]):
                _require_str(v, ["word", "meaning_ko", "example"], f"articles[{i}].vocab[{j}]")
            qs = a.get("questions")
            if not isinstance(qs, list) or len([q for q in qs if isinstance(q, str) and q.strip()]) < 5:
                raise ValueError(f"articles[{i}].questions 5개 미만")
    return _v


def build_article_embed(i: int, c: "art.Candidate", a: dict, today) -> dict:
    vocab = "\n".join(f"`{v['word'].strip()}` {v['meaning_ko'].strip()}\n　↳ *{v['example'].strip()}*"
                      for v in a["vocab"][:7])
    qs = [q.strip() for q in a["questions"] if isinstance(q, str) and q.strip()][:5]
    questions = "\n".join(f"**{n}.** {q}" for n, q in enumerate(qs, 1))
    if c.published:
        age = (today - c.published).days
        when = f"{c.published:%Y-%m-%d}" + (" (3개월 이전 기사)" if age > art.RECENT_DAYS else "")
    else:
        when = "날짜 미상"
    if c.level:
        when += f" · Level {c.level}"
    return {
        "title": clip(f"{i}. {c.title or c.url}", 256),
        "url": c.url,
        "color": art.SOURCE_COLORS.get(c.source, 0x99AAB5),
        "description": clip(f"**{art.SOURCE_LABELS.get(c.source, c.source)}** · {when}\n"
                            f"🏷️ **{a['topic_ko'].strip()}**\n{a['summary_ko'].strip()}", 4096),
        "fields": [
            {"name": "📝 Vocab 7", "value": clip(vocab, 1024), "inline": False},
            {"name": "💬 Small talk 5", "value": clip(questions, 1024), "inline": False},
        ],
    }


def send_discord_content(content: str, embeds: list[dict] | None = None) -> None:
    payload = {"username": "English Coach", "content": content, "embeds": embeds or []}
    if DRY_RUN:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL이 설정되지 않았습니다.")
    r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"Discord 발송 실패 HTTP {r.status_code}: {r.text[:300]}")


def run_articles() -> None:
    if not ARTICLE_WEIGHTS:
        raise RuntimeError("ARTICLE_WEIGHTS 설정이 비어 있습니다.")
    now_kst = datetime.now(KST)
    today = now_kst.date()
    sent = load_json_list(ARTICLES_HISTORY_PATH)
    used = {art.norm_url(x["url"]) for x in sent if x.get("url")}

    pools = art.collect_all(list(ARTICLE_WEIGHTS))
    picks = art.choose(pools, ARTICLE_WEIGHTS, used, k=3, today=today)
    if not picks:
        raise RuntimeError("모든 소스에서 새 기사를 찾지 못했습니다. --mode sources 로 점검해 주세요.")
    for c in picks:
        art.enrich(c)
        if not c.title:
            c.title = art.title_from_slug(c.url)

    blocks = []
    for i, c in enumerate(picks, 1):
        excerpt = (c.text or c.description or "(no text available — title only)")[:2500]
        blocks.append(f"[id {i}] source: {art.SOURCE_LABELS.get(c.source, c.source)}\n"
                      f"title: {c.title}\nurl: {c.url}\n"
                      f"date: {c.published or 'unknown'}\nexcerpt: {excerpt}")
    obj = generate_json(ARTICLE_SYSTEM, "\n\n".join(blocks), validate_articles(len(picks)))

    header = f"📰 **[오늘의 아티클 {len(picks)}선]** {today:%Y-%m-%d} ({WEEKDAYS_KO[today.weekday()]})"
    if len(picks) < 3:
        header += f"\n-# 새 기사가 부족해 {len(picks)}개만 골랐어요."
    for i, (c, a) in enumerate(zip(picks, obj["articles"]), 1):
        send_discord_content(header if i == 1 else "", [build_article_embed(i, c, a, today)])
        time.sleep(1)
    print(f"[Articles] {len(picks)}개 발송: " + ", ".join(c.source for c in picks))

    if not DRY_RUN:
        for c in picks:
            sent.append({"url": c.url, "title": c.title, "source": c.source,
                         "published": c.published.isoformat() if c.published else None,
                         "sent_date": today.isoformat()})
        save_json_list(ARTICLES_HISTORY_PATH, sent, 3000)


def run_sources() -> None:
    today = datetime.now(KST).date()
    print(f"가중치: {ARTICLE_WEIGHTS}")
    pools = art.collect_all(list(ARTICLE_WEIGHTS) or list(art.COLLECTORS))
    for s, lst in pools.items():
        recent = sum(1 for c in lst if art.tier(c, today) == 0)
        undated = sum(1 for c in lst if c.published is None)
        print(f"\n=== {s}: 후보 {len(lst)}개 (최근 3개월 {recent}, 날짜 미상 {undated})")
        for c in sorted(lst, key=lambda c: c.published or date.min, reverse=True)[:5]:
            print(f"  {c.published or '----------'}  {clip(c.title or '-', 70)}\n      {c.url}")


# ───────────────────────── main ─────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["prep", "review", "articles", "morning", "sources"], required=True)
    mode = parser.parse_args().mode
    if mode == "sources":
        run_sources()
        return
    jobs = {"prep": [("prep", run_prep)], "review": [("review", run_review)],
            "articles": [("articles", run_articles)],
            "morning": [("review", run_review), ("articles", run_articles)]}[mode]
    failed = False
    for label, fn in jobs:  # morning: 한쪽이 실패해도 다른 쪽은 발송
        try:
            fn()
        except Exception as e:
            print(f"[ERROR] {label}: {e}", file=sys.stderr)
            notify_failure(label, e)
            failed = True
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
