# English Coach Bot 🎯

평일 저녁 영어 수업 전에 구동사 치트시트를 Discord로 보내고, 다음 날 아침에 복습 카드를 보내는 봇입니다.
GitHub Actions와 Gemini(또는 Groq) 무료 티어만 사용하므로 비용은 $0입니다.

```
english-coach-bot/
├── .github/workflows/daily_prep.yml   # 스케줄러 (2개 cron + 수동 실행)
├── scripts/send_cheat_sheet.py        # 핵심 로직 (--mode prep | review)
├── data/history.json                  # 출제 기록 (자동 커밋, 중복 방지·복습용)
└── requirements.txt
```

| 시각 (KST) | 동작 | cron (UTC) |
|---|---|---|
| 월~금 18:40 | 치트시트 발송 | `40 9 * * 1-5` |
| 수업 후 | 튜터 피드백을 같은 채널에 붙여넣기 (수동) | — |
| 화~토 08:00 | 복습 카드 발송 | `0 23 * * 1-5` |

---

## 1. GitHub 레포 만들기

1. GitHub에서 새 레포를 만들고 이 폴더의 파일을 그대로 push합니다. 폴더 구조, 특히 `.github/workflows/`의 위치를 유지해야 합니다.
2. Public 레포는 Actions 사용 시간이 무제한입니다. Private 레포도 월 2,000분이 무료이고, 이 봇은 한 번 실행에 약 1분이 걸리므로 충분합니다.

## 2. Discord Webhook URL 만들기

1. Discord 서버에서 **서버 설정 → 연동(Integrations) → 웹후크 → 새 웹후크**를 누릅니다.
2. 이름을 정하고(예: English Coach) 메시지를 받을 채널을 선택합니다.
3. **웹후크 URL 복사**를 누릅니다. 이 값이 `DISCORD_WEBHOOK_URL`입니다.

> 웹후크 URL을 아는 사람은 누구나 채널에 글을 쓸 수 있습니다. 코드에 직접 넣지 말고 반드시 Secret으로만 보관하세요.

## 3. Gemini API 키 발급 (무료)

1. https://aistudio.google.com 에 Google 계정으로 로그인합니다.
2. **Get API key → Create API key**를 누르고 키를 복사합니다. 이 값이 `GEMINI_API_KEY`입니다.
3. 무료 티어는 입력 데이터가 Google의 서비스 개선에 쓰일 수 있습니다. 튜터 피드백에 개인정보가 섞이지 않게 주의하세요.

**(선택) Groq 폴백:** https://console.groq.com 에서 API 키를 만들어 `GROQ_API_KEY`로 등록하면, Gemini가 실패할 때 자동으로 Groq(Llama)를 사용합니다.

## 4. (선택) 튜터 피드백을 읽을 Discord 봇

웹후크는 메시지를 보내기만 하고 읽지는 못합니다. 복습 카드가 튜터 피드백을 반영하게 하려면 봇이 필요합니다.
봇이 없으면 전날 치트키 표현과 예전 표현 1개로 복습 카드를 만듭니다.

1. https://discord.com/developers/applications 에서 **New Application**을 만듭니다.
2. **Bot** 탭에서 **Reset Token**을 눌러 토큰을 복사합니다. 이 값이 `DISCORD_BOT_TOKEN`입니다.
3. 같은 탭에서 **MESSAGE CONTENT INTENT**를 켭니다. 이 설정이 꺼져 있으면 메시지 본문이 빈 값으로 옵니다.
4. **OAuth2 → URL Generator**로 이동합니다. Scope는 `bot`, 권한은 `View Channels`와 `Read Message History`를 선택하고, 생성된 URL로 봇을 서버에 초대합니다.
5. Discord 앱에서 **사용자 설정 → 고급 → 개발자 모드**를 켭니다. 그다음 채널을 우클릭해 **채널 ID 복사**를 누릅니다. 이 값이 `DISCORD_CHANNEL_ID`입니다.

피드백은 치트시트가 올라온 **같은 채널**에 붙여넣습니다. 스레드에 붙여넣으면 읽지 못합니다.
붙여넣은 글이 길어서 Discord가 자동으로 `message.txt` 첨부로 바꿔도 함께 읽습니다.

## 5. GitHub Secrets 등록

레포에서 **Settings → Secrets and variables → Actions → New repository secret**을 눌러 다음 값을 등록합니다.

| 이름 | 필수 여부 |
|---|---|
| `DISCORD_WEBHOOK_URL` | 필수 |
| `GEMINI_API_KEY` | 필수 (Groq만 쓸 경우 생략 가능) |
| `GROQ_API_KEY` | 선택 |
| `DISCORD_BOT_TOKEN` | 선택 |
| `DISCORD_CHANNEL_ID` | 선택 |

같은 화면의 **Variables** 탭에서 선택 설정을 할 수 있습니다.

- `GEMINI_MODEL`: 기본값은 `gemini-2.5-flash`입니다. 모델이 단종되면 여기서 바꾸세요.
- `GROQ_MODEL`: 기본값은 `llama-3.3-70b-versatile`입니다.
- `CLASS_TIME`: 기본값은 `19:20`이며 치트시트 제목에 표시됩니다.

## 6. 테스트

1. **Actions** 탭에서 **Daily English Coach → Run workflow**를 누르고 `prep`을 선택합니다. 치트시트가 도착하는지 확인합니다.
2. 같은 방법으로 `review`를 실행해 복습 카드가 오는지 확인합니다.
3. history 커밋 단계에서 push 권한 오류가 나면 **Settings → Actions → General → Workflow permissions**를 **Read and write**로 바꿉니다.

로컬에서 Discord 발송 없이 확인하려면 다음처럼 실행합니다.

```bash
pip install -r requirements.txt
DRY_RUN=1 GEMINI_API_KEY=... python scripts/send_cheat_sheet.py --mode prep
```

---

## 운영 시 알아둘 점

- **GitHub cron은 정시에 실행된다는 보장이 없습니다.** 부하가 많은 시간대에는 수 분에서 수십 분 늦게 실행되거나, 드물게 건너뛰어지기도 합니다. 그래서 치트시트는 수업 40분 전인 18:40으로 잡았습니다. 시간을 바꾸려면 첫 번째 cron만 수정하면 됩니다. 모드 판별은 review용 cron 문자열만 비교하므로 다른 곳은 고칠 필요가 없습니다.
- **실패 알림:** LLM 호출이나 발송이 실패하면 채널에 ⚠️ 알림이 옵니다. Discord 발송 자체가 실패한 경우에는 알림도 가지 않습니다.
- **공휴일에도 발송됩니다.** 필요하면 Actions 탭에서 워크플로를 잠시 **Disable**하세요.
- **자동 중지 방지:** Public 레포는 60일 동안 활동이 없으면 스케줄이 자동으로 꺼집니다. 이 봇은 평일마다 history를 커밋하므로 그 상태가 되지 않습니다.
