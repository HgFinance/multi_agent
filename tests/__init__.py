"""tests 를 패키지로 만든다.

▶ 왜 필요한가 (2026-08-11 실측)
  `tests/security/test_service_auth.py` 가 `from tests.security.service_auth_test_utils
  import make_token` 을 하는데, `tests/security/__init__.py` 만 있고 여기가 비어
  있으면 pytest 가 `tests/` 를 sys.path 에 넣고 모듈을 `security.test_service_auth`
  로 잡는다. 그러면 `tests` 라는 이름이 해석되지 않아 **수집 단계에서 죽고**,
  ImportError 하나가 스위트 전체를 멈춘다(실제로 152개가 통째로 안 돌았다).

  이 파일이 있으면 pytest 가 저장소 루트를 sys.path 에 넣고 모듈을
  `tests.security.test_service_auth` 로 잡아 import 가 성립한다.
  `__init__.py` 가 없는 다른 테스트 디렉터리의 수집 방식은 바뀌지 않는다.
"""
