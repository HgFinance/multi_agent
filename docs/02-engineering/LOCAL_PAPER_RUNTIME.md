# Local PAPER runtime

이 저장소의 현재 실행 대상은 배포용 인증 서비스가 아니라 로컬 모의투자 화면이다.

## 고정 경로

- 브라우저 로그인, Supabase Auth, 세션, JWT, OAuth 로그인 화면은 구현하지 않는다.
- 프론트는 `00000000-0000-4000-8000-00000000cec0` 고정 데모 ID를
  `X-User-Id`로 보낸다. 자격증명이나 쿠키는 사용하지 않는다.
- 브라우저는 동일 출처 `/bff/*`를 호출하고 Worker가 내부 BFF로 헤더를 전달한다.
  따라서 `8001/ui/me`를 헤더 없이 직접 호출했을 때의 401은 정상적인 내부 경계다.
- 로컬 BFF는 `npm run bff`로 실행한다. 이 명령은 `PAPER` 환경, `broker` 조회 모드,
  LS 시장·계좌·체결 조회를 명시적으로 켠다.
- 모의계좌는 로컬 `.env`의 `LS_ACCOUNT_NO_PAPER=5601`을 사용한다. AppKey/Secret은
  `.env`에만 두고 커밋하지 않는다.

## 데이터와 안전 경계

시장 상위종목은 LS `/stock/high-item`, 계좌·보유종목·체결 요약은 LS PAPER 조회에서
읽는다. 이 경로는 읽기 전용이며 주문 제출 workflow는 로컬 Compose에서 비활성화되어
있다. 브라우저가 Supabase Service Role, LS Credential, DB 연결을 직접 소유하지 않는다.

Compose와 이 문서가 다르면 `docker-compose.yml` 및 `package.json`의 `bff` 스크립트를
현재 기준으로 삼고, fixture/비활성 LS 설정을 되살리지 않는다.
