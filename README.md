# valuebot — 애널리스트 보고서 밸류에이션 역산 봇

증권사 보고서 PDF를 넣으면 **목표주가가 어떤 산식으로 도출됐는지 역산**하고,
그 계산이 실제로 맞는지 **파이썬으로 검산**합니다.

핵심 설계: LLM은 숫자를 *읽기만* 하고, 곱셈·합산은 전부 파이썬이 합니다.
(LLM 산수는 신뢰할 수 없고, 이 봇의 존재 이유가 바로 그 산수이기 때문)

---

## 1. Gemini API 키 발급 (전용 키)

기존 봇들(`tg-digest`, `sentiment_bot`, `pdf_bot`)과 **쿼터를 분리**해야 합니다.
PDF 분석은 토큰 소모가 커서 공유 키를 쓰면 다른 봇이 먼저 죽습니다.

1. https://aistudio.google.com/apikey 접속
2. **Create API key** → **Create API key in new project** 선택
   (기존 프로젝트 재사용 금지 — 무료 티어 쿼터는 프로젝트 단위)
3. 프로젝트 이름 예: `valuebot-quota`
4. 발급된 키 복사

무료 티어 기준 참고 (2026년 기준, 변동 가능):
- `gemini-2.5-flash` — 분당 요청·일일 요청 제한이 pro보다 훨씬 넉넉
- `gemini-2.5-pro` — 추출 정확도는 높으나 무료 일일 한도가 매우 타이트

기본 코드는 pro를 우선 시도합니다. 쿼터가 자주 터지면:
`VALUEBOT_MODEL=gemini-2.5-flash` 로 고정하세요.

## 2. 텔레그램 봇 생성

1. 텔레그램에서 `@BotFather` 검색
2. `/newbot`
3. 표시 이름: `밸류 역산 봇` (아무거나)
4. username: `mason_valuecheck_bot` 처럼 `_bot`으로 끝나야 함
5. 받은 토큰 복사
6. 추가 권장: `/setprivacy` → 해당 봇 → **Disable** (그룹에서도 파일 수신하려면)

## 3. GitHub 레포 설정

레포 생성 (`flyoflove-ai/valuebot`) 후 파일 업로드.
Actions 무료 분을 쓰려면 **Public** 으로 만드세요.

Settings → Secrets and variables → Actions → New repository secret

| 이름 | 값 |
|---|---|
| `VALUEBOT_TG_TOKEN` | BotFather 토큰 |
| `VALUEBOT_GEMINI_KEY` | 새로 발급한 Gemini 키 |
| `VALUEBOT_ALLOWED` | (선택) 본인 chat_id. 미설정 시 누구나 사용 가능 |

`VALUEBOT_ALLOWED`는 봇에 `/id` 를 보내면 나오는 숫자를 넣으면 됩니다.
Public 레포라 봇 username이 노출될 수 있으니 **설정을 권장**합니다.

## 4. 실행

Actions 탭 → `valuebot` → **Run workflow**

- 상주 롱폴링 루프로 5.5시간 동작 후 정상 종료, 6시간 cron이 재기동
- `concurrency: cancel-in-progress` 로 좀비 런 차단
- `timeout` 래퍼로 파이썬이 매달려도 OS 레벨 강제 종료

---

## 사용법

- **PDF 전송** → 자동 분석 (텔레그램 봇 다운로드 한도 20MB)
- **본문 텍스트 붙여넣기** (200자 이상) → 자동 분석
- `/help` `/model` `/id`

## 출력 항목

1. 방법론 판별 — PER / PBR / SOTP / DCF / EV·EBITDA
2. **계산식 역산 + 검산** — `EPS × 배수 = 목표주가` 를 복원해 실제 곱셈 검증
   - ✅ 일치 (±2%) / 🟡 근사 (±5%) / 🔴 불일치
   - 🔧 단위 환산 시 일치 — 억원·백만원 혼용으로 인한 괴리 자동 판별
3. 투입 변수 — EPS·BPS 값, 기준연도, EPS 정의(지배주주/희석/12M 선행)
4. 배수 산정 근거 — 과거 밴드 위치, Peer 셋, 프리미엄/할인 사유
5. 민감도 — 배수 ±2스텝 × EPS ±10% 그리드 (배수 크기에 비례한 스텝)
6. 실적 가정 / 핵심 전제
7. ⚠️ 미기재 항목 — 보고서가 밝히지 않은 것
8. 🔍 검증 포인트 — 공격적 가정, 논리적 비약

### PBR 보고서 추가 기능
`(ROE − g) / (COE − g)` 이론 PBR을 계산해 **적용배수의 프리미엄/디스카운트 폭**을 표시합니다.

### DCF 보고서 추가 기능
`WACC − g` 스프레드가 3%p 미만이면 터미널밸류 민감도 경고를 띄웁니다.

---

## 알려진 한계

- 목표주가 산식이 본문에 아예 없는 보고서(요약 노트, 산업 리포트)는 역산 불가 → 그렇게 표시됨
- 스캔 이미지 PDF는 Gemini OCR 품질에 의존
- 검산 🔴 이 곧 "보고서가 틀렸다"는 뜻은 아님. 우선주·자사주 조정, 반올림, 미기재 변수가 흔한 원인
- **로컬에서 실제 Gemini/텔레그램 API 호출 검증은 하지 못했습니다.** 렌더링·검산·단위환산 로직은 모의 데이터로 테스트 완료. 실제 PDF 첫 투입 시 결과를 원문과 한 번 대조해 보시길 권합니다.
