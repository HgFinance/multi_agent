"""재현성 - 이 결과를 다시 만들 수 있는가.

담당: 재일 (퀀트·백테스트본부 QNT)
근거: contracts/quant_v2.ExperimentCardV1
      (dataset_manifest_id, dataset_hash, code_hash, dependency_lock_hash,
       seed, lineage)

▶ 무엇이 문제였나
  ① **code_hash 가 파일 하나만 봤다.** backtest_runner.code_version() 은
     backtest_runner.py 자기 자신만 해싱한다. 그런데 결과는 walk_forward,
     config_binding, overfit_stats, pbo_cscv, trial_family 에도 달려 있다 -
     그중 아무거나 고쳐도 숫자가 바뀌는데 code_hash 는 그대로다.
     **"코드가 같은데 결과가 다르다" 로 보이게 만든다** - 재현성 기록이
     거짓말을 하면 없느니만 못하다.

  ② **dependency_lock_hash 가 아예 없었다.** requirements.txt 는 버전을
     고정하지 않는다(`python-dotenv` 처럼 이름만). 그 파일을 해싱하면
     안정된 해시가 나오지만 **실제 설치본은 다를 수 있다** - 고정됐다는
     착각만 준다. 선언이 아니라 **설치된 버전**을 본다.

▶ 모르면 모른다고 적는다
  버전을 못 읽은 패키지는 건너뛰지 않고 "unknown" 으로 남긴다. 건너뛰면
  psycopg2 가 있는 환경과 없는 환경이 **같은 해시**가 되어, 서로 다른
  환경을 같다고 기록한다.

자체 점검: python departments/04-quant-backtest/pipeline/reproducibility.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

MODULE_VERSION = "quant-reproducibility-v1"

# ▶ 결과에 영향을 주는 실행면. 여기 있는 파일이 하나라도 바뀌면 code_hash 가
#   바뀌어야 한다 - 한 파일만 해싱하던 것이 문제였다.
CODE_SURFACE_DIR = Path(__file__).resolve().parent
CODE_SURFACE_GLOB = "*.py"

# 런타임에 실제로 쓰는 패키지. **설치된 버전**을 읽는다(선언이 아니라).
RUNTIME_PACKAGES = ("psycopg2-binary", "pydantic", "python-dotenv",
                    "fastapi", "uvicorn")

# 버전을 못 읽었을 때 적는 값. **건너뛰지 않는다** - 건너뛰면 있는 환경과
# 없는 환경이 같은 해시가 된다.
UNKNOWN = "unknown"


def _sha(payload) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def code_surface() -> dict:
    """실행면 파일별 해시. **자기 자신도 포함한다** - 이 파일의 규칙이
    바뀌어도 재현성이 달라진다."""
    out = {}
    for p in sorted(CODE_SURFACE_DIR.glob(CODE_SURFACE_GLOB)):
        if p.name == "__init__.py":
            continue
        out[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    return out


def code_hash() -> str:
    """실행면 전체의 해시. 어느 모듈을 고쳐도 값이 바뀐다."""
    return _sha(code_surface())


def installed_versions(packages=None) -> dict:
    """**설치된** 버전. 선언(requirements.txt)이 아니다.

    packages 기본값을 인자 자리에 두지 않는다 - 파이썬은 기본 인자를 def
    시점에 한 번만 평가하므로, RUNTIME_PACKAGES 를 바꿔도 반영되지 않아
    **설정 가능해 보이는데 실제로는 안 되는** 상태가 된다(실측으로 걸렸다).

    requirements.txt 는 버전을 고정하지 않으므로 해싱해도 환경이 같다는
    보장이 없다 - 고정됐다는 착각만 준다.
    """
    from importlib.metadata import PackageNotFoundError, version

    out = {}
    for name in (RUNTIME_PACKAGES if packages is None else packages):
        try:
            out[name] = version(name)
        except PackageNotFoundError:
            # 건너뛰지 않는다 - 건너뛰면 있는 환경과 없는 환경이 같은 해시다
            out[name] = UNKNOWN
        except Exception:  # noqa: BLE001
            out[name] = UNKNOWN
    out["python"] = ".".join(str(x) for x in sys.version_info[:3])
    return out


def dependency_lock_hash() -> str:
    """설치본 기준 의존성 해시."""
    return _sha(installed_versions())


def unresolved_packages(versions: dict | None = None) -> list:
    """버전을 못 읽은 패키지. **비어 있지 않으면 재현성이 부분적이다.**"""
    v = versions if versions is not None else installed_versions()
    return sorted(k for k, x in v.items() if x == UNKNOWN)


def lineage(*, dataset_name: str, dataset_version: str,
            manifest_id: str, source_versions=None) -> dict:
    """이 결과가 무엇에서 나왔는가. **값은 전부 문자열이다**(계약이 dict[str,str]).

    빈 값을 넣지 않는다 - 빈 문자열은 "없다" 와 "안 적었다" 를 구분 못 한다.
    """
    out = {
        "dataset": f"{dataset_name}/{dataset_version}",
        "dataset_manifest_id": str(manifest_id),
        "code_surface": f"{len(code_surface())} modules",
        "runtime": "python " + ".".join(str(x) for x in sys.version_info[:3]),
    }
    for k, v in (source_versions or {}).items():
        if v is None or str(v).strip() == "":
            continue                     # 빈 값은 적지 않는다
        out[f"source:{k}"] = str(v)
    return out


def snapshot(*, dataset_name: str, dataset_version: str, manifest_id: str,
             dataset_hash: str, seed: int, source_versions=None) -> dict:
    """ExperimentCard 의 재현성 블록. **못 채운 것은 표시한다.**"""
    vers = installed_versions()
    unresolved = unresolved_packages(vers)
    out = {
        "dataset_manifest_id": str(manifest_id),
        "dataset_hash": str(dataset_hash),
        "code_hash": code_hash(),
        "dependency_lock_hash": _sha(vers),
        "seed": int(seed),
        "lineage": lineage(dataset_name=dataset_name,
                           dataset_version=dataset_version,
                           manifest_id=manifest_id,
                           source_versions=source_versions),
    }
    if unresolved:
        # ▶ 조용히 넘기지 않는다. 이 실험은 "다시 만들 수 있다" 가 아니라
        #   "일부 의존성은 모른 채 기록됐다" 이다.
        out["reproducibility_gaps"] = unresolved
    return out


# ── 자체 점검 ────────────────────────────────────────────────────────────────

def _check_code_hash_covers_whole_surface():
    """**파일 하나가 아니라 실행면 전체를 본다.**

    예전 code_version() 은 backtest_runner.py 만 해싱해, walk_forward 나
    pbo_cscv 를 고쳐도 값이 그대로였다 - "코드가 같은데 결과가 다르다" 로
    보이게 만든다.
    """
    s = code_surface()
    assert len(s) >= 10, s
    for must in ("backtest_runner.py", "walk_forward.py", "pbo_cscv.py",
                 "trial_family.py", "config_binding.py", "overfit_stats.py"):
        assert must in s, (must, sorted(s))
    # 자기 자신도 포함한다 - 이 규칙이 바뀌어도 재현성이 달라진다
    assert "reproducibility.py" in s


def _check_any_module_change_moves_hash():
    """어느 모듈이 바뀌어도 code_hash 가 움직인다."""
    base = code_surface()
    for target in ("walk_forward.py", "pbo_cscv.py"):
        moved = dict(base)
        moved[target] = "0" * 16
        assert _sha(moved) != _sha(base), target


def _check_runtime_packages_is_actually_configurable():
    """**기본 인자를 def 시점에 묶지 않는다.**

    묶으면 RUNTIME_PACKAGES 를 바꿔도 반영되지 않아 설정 가능해 보이는데
    실제로는 안 되는 상태가 된다 - 실측으로 걸렸다.
    """
    import reproducibility as R

    orig = R.RUNTIME_PACKAGES
    try:
        R.RUNTIME_PACKAGES = ("definitely-not-installed-xyz",)
        v = R.installed_versions()
        assert set(v) == {"definitely-not-installed-xyz", "python"}, v
    finally:
        R.RUNTIME_PACKAGES = orig


def _check_unknown_is_recorded_not_skipped():
    """**건너뛰면 있는 환경과 없는 환경이 같은 해시가 된다.**"""
    v = installed_versions(("psycopg2-binary", "definitely-not-installed-xyz"))
    assert v["definitely-not-installed-xyz"] == UNKNOWN, v
    assert "definitely-not-installed-xyz" in unresolved_packages(v), v
    # 없는 패키지가 하나 더 늘면 해시가 달라져야 한다
    v2 = installed_versions(("psycopg2-binary", "definitely-not-installed-xyz",
                             "also-not-installed-xyz"))
    assert _sha(v) != _sha(v2), (v, v2)


def _check_gaps_are_surfaced():
    """못 채운 것을 조용히 넘기지 않는다."""
    import reproducibility as R

    orig = R.RUNTIME_PACKAGES
    try:
        R.RUNTIME_PACKAGES = ("definitely-not-installed-xyz",)
        s = R.snapshot(dataset_name="d", dataset_version="v", manifest_id="m",
                       dataset_hash="sha256:aa", seed=0)
        assert s["reproducibility_gaps"] == ["definitely-not-installed-xyz"], s
    finally:
        R.RUNTIME_PACKAGES = orig


def _check_lineage_values_are_strings():
    """계약이 dict[str, str] 이다 - 숫자를 넣으면 카드 생성이 죽는다."""
    ln = lineage(dataset_name="krx", dataset_version="v2", manifest_id="ds-1",
                 source_versions={"krx": 3, "dart": None, "blank": "  "})
    assert all(isinstance(v, str) for v in ln.values()), ln
    assert ln["source:krx"] == "3"
    # 빈 값은 적지 않는다 - "없다" 와 "안 적었다" 를 구분 못 한다
    assert "source:dart" not in ln and "source:blank" not in ln, ln


def _check_snapshot_is_deterministic():
    """같은 환경이면 같은 값이다 - 매번 달라지면 재현성 기록이 아니다."""
    kw = dict(dataset_name="krx", dataset_version="v2", manifest_id="ds-1",
              dataset_hash="sha256:aa", seed=42)
    assert snapshot(**kw) == snapshot(**kw)


def _check_declared_requirements_are_not_the_lock():
    """**requirements.txt 를 lock 으로 쓰지 않는다.**

    버전을 고정하지 않으므로 해싱해도 환경이 같다는 보장이 없다 - 고정됐다는
    착각만 준다. 이 모듈은 설치된 버전을 읽는다.
    """
    src = Path(__file__).read_text(encoding="utf-8")
    body = src.split("# ── 자체 점검")[0]
    assert "requirements.txt" not in body.replace(
        "requirements.txt 는", "").replace("requirements.txt)", ""), \
        "requirements.txt 를 실제로 읽으면 안 된다(주석 언급은 허용)"
    assert "importlib.metadata" in body


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{MODULE_VERSION} 자체 점검 (DB 없음)")
    _check_code_hash_covers_whole_surface();  print("  실행면 전체 해싱        OK")
    _check_any_module_change_moves_hash();    print("  모듈 변경 -> 해시 이동   OK")
    _check_runtime_packages_is_actually_configurable()
    print("  기본인자 미고정         OK")
    _check_unknown_is_recorded_not_skipped(); print("  미상 기록(건너뛰기X)    OK")
    _check_gaps_are_surfaced();               print("  결손 표면화             OK")
    _check_lineage_values_are_strings();      print("  lineage 문자열 계약     OK")
    _check_snapshot_is_deterministic();       print("  결정론                  OK")
    _check_declared_requirements_are_not_the_lock(); print("  선언 != lock          OK")
    print("재현성 8개 영역 통과.")
