# English Coach Bot 🎯

영어 회화 수업을 위한 Discord 봇입니다. 비용은 $0이에요.

| 시각 (KST) | 모드 | 보내는 것 |
|---|---|---|
| 월~금 18:40 | `prep` | 🎯 오늘의 구동사 치트시트 |
| 화~토 08:00 | `morning` | 🌅 복습 카드 + 📰 오늘의 아티클 3선 (각각 별도 메시지) |

정확한 시각에 실행되도록 **cron-job.org**가 GitHub Actions를 호출합니다. GitHub 자체 예약은 몇 시간씩 밀릴 수 있어서 쓰지 않아요.

```
english-coach-bot/
├── .github/workflows/daily_prep.yml   # 실행 정의 (수동·외부 호출 전용)
├── scripts/send_cheat_sheet.py        # 메인 (모드별 실행, LLM, Discord 발송)
├── scripts/articles.py                # 기사 수집·가중치 선택
├── data/history.json                  # 보낸 구동사 기록
├── data/articles_history.json         # 보낸 기사 기록 (중복 방지)
└── requirements.txt
```

---

## 오늘의 아티클

- 세 사이트에서 가중치에 따라 무작위로 3개를 고릅니다. 기본 가중치는 Engoo 0.5, 서울경제 영문판 0.3, TED 0.2예요.
- 최근 3개월 기사를 우선 고르고, 부족하면 날짜를 모르는 기사, 그다음 오래된 기사 순으로 고릅니다.
- 한 번 보낸 링크는 `articles_history.json`에 기록돼서 다시 나오지 않아요.
- 기사마다 주제, 2문장 요약, vocab 7개, 스몰톡 질문 5개를 붙여서 보냅니다.

**소스 점검:** Actions → Run workflow → `sources`로 실행하면, 각 사이트에서 기사를 몇 개 찾았는지 로그로만 보여줍니다. 발송은 하지 않아요. 사이트 구조가 바뀌어 기사가 안 잡힐 때 이걸로 확인하세요.

**Engoo가 0개로 나올 때:** Engoo는 자바스크립트 앱이라 기사 목록을 못 찾을 수 있어요. 크롬 개발자 도구(F12 → Network → Fetch/XHR)에서 기사 목록 JSON 주소를 찾은 뒤, Variable `ENGOO_LIST_URL`에 넣으면 그 주소를 사용합니다. Engoo에서 하나도 못 찾으면 나머지 두 사이트의 가중치로 자동 재분배돼요.

---

## 설정값

**Secrets** (Settings → Secrets and variables → Actions → Secrets)

| 이름 | 용도 |
|---|---|
| `DISCORD_WEBHOOK_URL` | 필수 — 발송 채널 |
| `GEMINI_API_KEY` | 필수 (Groq만 쓸 경우 생략 가능) |
| `GROQ_API_KEY` | 권장 — Gemini 실패 시 폴백 |
| `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID` | 선택 — 복습 카드에 튜터 피드백 반영 |

**Variables** (같은 화면의 Variables 탭, 모두 선택)

| 이름 | 기본값 | 설명 |
|---|---|---|
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | 모델이 단종되면 여기서 변경 |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | |
| `LLM_ORDER` | `gemini,groq` | Gemini가 자주 붐비면 `groq,gemini`로 바꿔 Groq를 먼저 시도 |
| `ARTICLE_WEIGHTS` | `engoo:0.5,sedaily:0.3,ted:0.2` | 기사 소스 가중치 |
| `ENGOO_LIST_URL` | (없음) | Engoo 목록 JSON 주소 |
| `CLASS_TIME` | `19:20` | 치트시트 제목에 표시되는 수업 시각 |

**Settings → Actions → General → Workflow permissions**는 **Read and write**로 설정해야 합니다.

---

## cron-job.org 설정

### 1. GitHub 토큰 발급
1. GitHub 오른쪽 위 프로필 → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**으로 이동합니다.
2. Token name은 `cron-job`, Expiration은 원하는 기간으로 정합니다. 만료일은 캘린더에 적어두세요. 만료되면 실행이 멈춥니다.
3. Repository access는 **Only select repositories → english-coach-bot**을 선택합니다.
4. Permissions → Repository permissions에서 **Actions**를 **Read and write**로 설정합니다.
5. **Generate token**을 누르고 토큰(`github_pat_...`)을 복사합니다.

### 2. cron-job.org 작업 두 개 만들기
1. cron-job.org에 가입하고, 계정 설정에서 시간대를 **Asia/Seoul**로 맞춥니다.
2. **CREATE CRONJOB**을 누르고 아래처럼 입력합니다.

| 항목 | 치트시트 작업 | 아침 작업 |
|---|---|---|
| Title | `English prep` | `English morning` |
| URL | (아래 URL, 두 작업 공통) | 같음 |
| Schedule | Custom: 월~금, 18시 40분 | Custom: 화~토, 8시 0분 |
| Request body | `{"ref":"main","inputs":{"mode":"prep"}}` | `{"ref":"main","inputs":{"mode":"morning"}}` |

URL:
```
https://api.github.com/repos/neurosoeunpark/english-coach-bot/actions/workflows/daily_prep.yml/dispatches
```

**ADVANCED** 탭에서는 두 작업 모두 똑같이 설정합니다.
- Request method: **POST**
- Headers 4개:
  - `Accept`: `application/vnd.github+json`
  - `Authorization`: `Bearer 여기에_토큰`
  - `X-GitHub-Api-Version`: `2022-11-28`
  - `Content-Type`: `application/json`

3. 저장 후 **TEST RUN**을 눌러 응답 코드가 **204**면 성공입니다. GitHub Actions 탭에 실행이 하나 생겨요.

| 응답 코드 | 의미 |
|---|---|
| 204 | 정상 |
| 401 | 토큰이 틀렸거나 만료됨 |
| 403 | 토큰에 Actions 쓰기 권한이 없음 |
| 404 | URL 오타 또는 토큰이 이 레포에 접근할 수 없음 |
| 422 | body 형식 오류 (`ref`나 `mode` 값 확인) |

---

## 운영 메모

- **실패 알림:** LLM이나 기사 수집이 실패하면 채널에 ⚠️ 알림이 옵니다. `morning`은 복습 카드와 아티클 중 하나가 실패해도 나머지는 발송돼요.
- **공휴일에도 발송됩니다.** 쉬고 싶은 날은 cron-job.org에서 작업을 잠시 끄세요.
- **피드백은 채널에 직접 붙여넣어야 합니다.** 스레드에 붙여넣으면 봇이 읽지 못해요.
