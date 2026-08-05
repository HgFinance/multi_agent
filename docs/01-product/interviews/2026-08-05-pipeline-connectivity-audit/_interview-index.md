# Pipeline Connectivity Audit Interview

- Topic: 부서 간 연결, 직원 LangGraph 실행, 전체 파이프라인 결과, 대시보드 고도화
- Date: 2026-08-05
- Status: 완료
- Output: [baseline-audit.md](baseline-audit.md)

## Interview log

| Round | Status | Note |
|---|---|---|
| 0 | Captured | 사용자가 전사 연결성과 대시보드 고도화 가능성을 질문함 |
| 1 | Captured | 추천 자문과 Paper 폐쇄 루프를 함께 추진하고, 관제·결과 대시보드를 단계적으로 고도화하며, 로컬 TEST 후 실제 인프라로 확장하기로 함 |
| 2 | Captured | Paper Order부터 Fill·Ledger·Position·NAV projection까지 검증하고, 대표 Dashboard와 운영자 Operations Console을 분리하기로 함 |
| 3 | Captured | 대표의 명시적 승인 후에만 Paper Order를 제출하기로 함 |
| 4 | Captured | 채팅 가독성 개선, 고급 설정 정리, 초기 상태의 백엔드 반영 여부 표시, 분석 시작 불가 및 Governance 500 오류 수정을 요청함 |
| 5 | Captured | LangGraph 실행 오류 원인 수정, 대시보드 중첩 최소화, Risk·QA뿐 아니라 8개 전체 부서의 실행 상태 표시를 요청함 |
| 6 | Captured | 실행 오류 원인(AttributeError) 수정, 부서 내부 통신과 직원별 작업 중·대기 상태를 추적할 수 있는 UI 고도화를 요청함 |
| 7 | Captured | 직원 추적 확인과 오류 재수정을 요청하고, 저장소 `.env`의 LangSmith 설정을 backend·frontend 관제에 안전하게 반영하는 것을 허용함 |
| 8 | Captured | Operation Console에 운영 핵심 정보를 모으고, LangSmith 원문 대신 정량 관측성을 표시하며, 부서 버튼으로 Risk·Research 등 내부 Worker 상태를 탐색하기를 요청함 |
