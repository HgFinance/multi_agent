# Source Garbage Collection

검수일: 2026-08-28 UTC

정적 호출자와 테스트 참조를 분리해 확인한 뒤, 실제로 어느 경로에서도 사용하지 않는
소스만 제거했다. 동적 import나 외부 배포 스크립트까지 정적으로 증명하는 도구는 아니므로
호환성 표면은 자동 삭제하지 않는다.

| 후보 | 운영 소스 참조 | 테스트 참조 | 조치 |
|---|---:|---:|---|
| `apps/api/fact_router.py` | 0 | `tests/api/test_krx_alphanumeric_symbol_contract.py` | 유지·재검토 |
| `apps/api/ceo_hermes_client.py` | 0 | `tests/api/test_ceo_d5_wiring.py`, `tests/api/test_ceo_hermes_boundary.py` | 유지·재검토 |
| `departments/02-trading/contracts/packet_gate.py` | 0 | 0 | 제거 |

`packet_gate.py`만 작업 트리에서 제거했다. 삭제된 파일은 Git 이력으로 복구할 수 있다.
나머지 두 모듈은 운영 호출자는 없지만 테스트가 직접 기능을 검증하고 있으므로, 테스트를
지우는 방식의 정리는 하지 않았다.

재검증:

```bash
python scripts/source_garbage_collector.py
python scripts/source_garbage_collector.py --write
pytest -q tests/scripts/test_source_garbage_collector.py
```

판정 기준은 다음과 같다.

- 운영 소스와 테스트에 참조가 모두 0인 후보만 `REMOVED`로 판정한다.
- 테스트가 남아 있으면 구현이 운영에서 직접 호출되지 않더라도 `REVIEW`로 남긴다.
- 외부 배포 파일, 동적 import, 런타임 문자열 로딩은 사람이 별도로 확인한다.
