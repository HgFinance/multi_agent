"""실제 서비스 앱(`apps.api.main.app`)에 경로 중복 등록이 없는지 고정한다.

## 왜 이 테스트가 있나

2026-08-14, `ceo_mirror_router`와 `ceo_router`가 둘 다 `POST /ui/ceo/ask`를
등록하고 있었다. FastAPI는 같은 (path, method) 조합이 여러 라우터에 있어도
에러를 내지 않고 **먼저 등록된 쪽이 조용히 이긴다** - 나중에 등록된 쪽은
`app.routes`에 남아있지만 절대 실행되지 않는 죽은 코드가 된다.

이 사고에서는 `ceo.py`의 `ceo_query`(진짜 구현, Mandate `fund_id` 배선까지
정확히 들어간 코드)가 죽은 코드였고, 실제로 실행된 건 `ceo_mirror_api.py`가
독자적으로 재구성한 요청 모델이었다 - `ceo.py`만 읽으면 정상으로 보였지만
그 코드는 애초에 안 불렸다. `ceo.py`를 아무리 정확히 고쳐도 이 상태에서는
재발했을 것이다.

같은 경로를 두 라우터가 나눠 갖고 등록 순서로 승부하는 구조 자체를 막는다 -
어느 한쪽 코드가 옳은지가 아니라, 애초에 경합이 발생할 수 없게 한다.
"""

from __future__ import annotations

import unittest
from collections import Counter

from apps.api.main import app


class NoDuplicateRouteRegistrationTest(unittest.TestCase):
    def test_no_path_and_method_is_registered_twice(self) -> None:
        combos: list[tuple[str, str]] = []
        for route in app.routes:
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None)
            if not path or not methods:
                continue
            for method in methods:
                combos.append((path, method))

        counts = Counter(combos)
        duplicates = {combo: count for combo, count in counts.items() if count > 1}
        self.assertFalse(
            duplicates,
            "같은 (path, method)가 여러 라우터에 등록되면 등록 순서로 승부가 갈리고, "
            "지지 않은 쪽 코드는 있어도 절대 실행되지 않는 죽은 코드가 된다. "
            f"중복: {duplicates}",
        )

    def test_ceo_ask_is_owned_by_mirror_only(self) -> None:
        """가장 최근에 이 문제가 실제로 터졌던 경로를 명시적으로도 고정한다."""

        owners = [
            route
            for route in app.routes
            if getattr(route, "path", None) == "/ui/ceo/ask"
            and "POST" in getattr(route, "methods", set())
        ]
        self.assertEqual(
            len(owners),
            1,
            "POST /ui/ceo/ask는 ceo_mirror_api.mirror_ask 하나만 소유해야 한다.",
        )
        self.assertEqual(owners[0].endpoint.__module__, "apps.api.ceo_mirror_api")


if __name__ == "__main__":
    unittest.main()
