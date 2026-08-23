#!/usr/bin/env python3
"""고정 프론트엔드 테스트 계정 체인이 실 DB와 **일치하는지** 대조한다.

## 왜 필요한가

플레이스홀더 회원과 그 사용자가 소유하는 Fund가 한동안 별도로 관리돼,
`supabase db reset`을 하면 프론트엔드가 하드코딩한 `fund_id`가 존재하지 않는 행을
가리키는데 reset을 안 하니 드러나지 않았다. `governance.fund_memberships`가 0건이라 RLS 함수
`governance.can_access_fund()`가 service_role 외 전부 false를 뱉고 있던 것도
같은 이유로 묻혀 있었다.

**이 스크립트는 그 어긋남을 실행 한 번으로 드러낸다.** 세 곳을 대조한다:

  1. `.env`의 `DISCORD_ACTOR_MAP` 첫 binding (프론트엔드·Discord 공통 fixture 설정)
  2. 실 DB의 `auth.users` / `governance.user_profiles` / `accounting.funds`
  3. 실 DB의 `governance.fund_memberships` (소유 관계)

## 무엇을 하지 않나

**쓰기를 하지 않는다.** 순수 SELECT만 한다 - 어긋남을 고치는 것은 `seed.sql`을
고쳐 `supabase db reset`으로 재현하는 경로여야 하고, 이 스크립트가 직접 INSERT하면
"어디서 들어온 행인지 모르는 데이터"가 또 생긴다(이번 문제의 재발).

Mandate 내용도 검사하지 않는다. Mandate는 사용자가 프론트엔드에서 직접 만드는
데이터라 "있어야 할 값"이 정해져 있지 않다.

## 실행

    python scripts/check_test_user_wiring.py

DSN은 `.env`의 `DATABASE_URL`을 쓴다. `load_dotenv()`가 없는 저장소 관례를 따라
이 파일이 직접 `.env`를 읽는다(export 없이 바로 실행 가능). `--dsn-var`로 다른
환경변수 이름을 지정할 수 있다.

종료 코드: 전부 일치하면 0, 하나라도 어긋나면 1 (CI에 걸 수 있다).
"""
from __future__ import annotations

import argparse
import io
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ACCOUNTS_TS = REPO_ROOT / "ai-office" / "app" / "lib" / "currentAccount.ts"
_ACTOR_MAP_ENTRY_RE = re.compile(
    r"^\d{15,25}:([0-9a-fA-F-]{36}):([0-9a-fA-F-]{36})$"
)


def read_env_value(name: str) -> str | None:
    """`.env`에서 값 하나를 읽는다. 이미 프로세스 환경에 있으면 그쪽이 우선한다.

    배포 환경 값이 항상 파일보다 우선해야 한다(`departments/00-ceo-office/api/app.py`의
    `load_dotenv(override=False)`와 같은 정책).
    """

    existing = os.getenv(name)
    if existing and existing.strip():
        return existing.strip()
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return None
    for line in io.open(env_path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == name:
            return value.strip().strip('"').strip("'") or None
    return None


def parse_frontend_accounts() -> list[dict[str, str | None]]:
    """`DISCORD_ACTOR_MAP`의 첫 유효 binding을 프론트 fixture 계정으로 읽는다.

    프론트가 이 환경 변수에서 같은 값을 받는지 소스도 함께 확인한다. map은 Discord
    작성자마다 여러 entry를 가질 수 있지만, 고정 프론트 fixture는 첫 유효 3칸 binding
    하나만 쓴다.
    """

    if not ACCOUNTS_TS.exists():
        raise SystemExit(f"프론트엔드 계정 파일을 찾지 못했다: {ACCOUNTS_TS}")
    source = io.open(ACCOUNTS_TS, encoding="utf-8").read()
    if "DISCORD_ACTOR_MAP" not in source:
        raise SystemExit(f"{ACCOUNTS_TS}가 DISCORD_ACTOR_MAP을 참조하지 않는다")

    raw = read_env_value("DISCORD_ACTOR_MAP")
    if not raw:
        raise SystemExit("DISCORD_ACTOR_MAP이 비어 있다")
    for entry in re.split(r"[,\s]+", raw):
        match = _ACTOR_MAP_ENTRY_RE.fullmatch(entry.strip())
        if match:
            user_id, fund_id = match.groups()
            return [{"user_id": user_id, "label": "fixture", "fund_id": fund_id}]
    raise SystemExit("DISCORD_ACTOR_MAP에 유효한 discord_id:user_id:fund_id binding이 없다")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn-var",
        default="DATABASE_URL",
        help="DSN을 담은 환경변수 이름 (기본 DATABASE_URL)",
    )
    args = parser.parse_args()

    dsn = read_env_value(args.dsn_var)
    if not dsn:
        print(f"FAIL  {args.dsn_var}가 비어 있다 (.env 또는 환경변수 확인)")
        return 1

    try:
        import psycopg2
    except ImportError:
        print("FAIL  psycopg2가 없다 - pip install -r requirements.txt")
        return 1

    accounts = parse_frontend_accounts()
    print(f"프론트엔드 계정 {len(accounts)}건 ({ACCOUNTS_TS.relative_to(REPO_ROOT)})\n")

    try:
        connection = psycopg2.connect(dsn, connect_timeout=10)
    except Exception as exc:  # noqa: BLE001 - 진단 스크립트는 원인을 그대로 보여준다.
        print(f"FAIL  DB 접속 실패 ({args.dsn_var}): {str(exc).splitlines()[0]}")
        return 1

    problems: list[str] = []
    with connection, connection.cursor() as cursor:
        for account in accounts:
            user_id = account["user_id"]
            fund_id = account["fund_id"]
            label = account["label"]
            print(f"[{label}] user_id={user_id}")

            cursor.execute("select 1 from auth.users where id = %s", (user_id,))
            if cursor.fetchone() is None:
                problems.append(f"{label}: auth.users에 {user_id} 없음")
                print("  auth.users            MISSING")
            else:
                print("  auth.users            ok")

            cursor.execute(
                "select display_name, status from governance.user_profiles where user_id = %s",
                (user_id,),
            )
            row = cursor.fetchone()
            if row is None:
                problems.append(f"{label}: governance.user_profiles에 {user_id} 없음")
                print("  user_profiles         MISSING")
            else:
                print(f"  user_profiles         ok (status={row[1]})")

            if fund_id is None:
                print("  fund                  화면이 fundId=null로 선언 - 대조 생략")
                print()
                continue

            cursor.execute(
                "select fund_code, base_currency, status from accounting.funds where fund_id = %s",
                (fund_id,),
            )
            fund = cursor.fetchone()
            if fund is None:
                problems.append(
                    f"{label}: accounting.funds에 {fund_id} 없음 "
                    "(프론트엔드가 존재하지 않는 Fund를 가리킨다)"
                )
                print(f"  funds                 MISSING ({fund_id})")
            else:
                print(f"  funds                 ok ({fund[0]}, {fund[1]}, {fund[2]})")

            cursor.execute(
                """
                select role, status from governance.fund_memberships
                 where fund_id = %s and user_id = %s
                """,
                (fund_id, user_id),
            )
            memberships = cursor.fetchall()
            if not memberships:
                problems.append(
                    f"{label}: fund_memberships에 소유 관계 없음 "
                    "(can_access_fund()가 false를 준다)"
                )
                print("  fund_memberships      MISSING")
            else:
                roles = ", ".join(f"{r}/{s}" for r, s in memberships)
                print(f"  fund_memberships      ok ({roles})")

            # Mandate는 사용자가 직접 만드는 데이터라 없어도 오류가 아니다.
            # 다만 "몇 건인지"는 보여준다 - 한 Fund에 2건 이상이면
            # `GET /governance/v1/mandates/by-fund/{id}/current`가 409로 닫힌다.
            cursor.execute(
                "select count(*) from governance.mandates where fund_id = %s", (fund_id,)
            )
            mandate_count = cursor.fetchone()[0]
            if mandate_count > 1:
                problems.append(
                    f"{label}: fund_id={fund_id}에 Mandate가 {mandate_count}건 "
                    "- by-fund 조회가 409로 닫힌다"
                )
                print(f"  mandates              {mandate_count}건 AMBIGUOUS")
            else:
                print(f"  mandates              {mandate_count}건")
            print()

    print("-" * 60)
    if problems:
        print(f"어긋남 {len(problems)}건:")
        for problem in problems:
            print(f"  - {problem}")
        print("\n고치는 방법: supabase/seed.sql을 고치고 `supabase db reset`으로 재현한다.")
        print("이 스크립트는 쓰기를 하지 않는다 - 출처를 모르는 행을 또 만들지 않기 위해서다.")
        return 1
    print("전부 일치한다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
