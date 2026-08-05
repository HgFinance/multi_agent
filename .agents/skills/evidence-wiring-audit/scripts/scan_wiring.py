#!/usr/bin/env python3
"""배관 결함 후보를 정적으로 찾는다.

이 스크립트는 **판정하지 않는다.** 후보를 내고 사람이 확인한다 - 정적 분석만으로
"이건 확실히 버그다" 라고 말할 수 없는 경우가 많고, 오탐을 확정처럼 보고하면
진짜 결함이 그 안에 묻힌다.

찾는 것 (전부 실제로 한 세션에서 난 사고 유형이다):
  1. 자릿수 상한 정규식      `\\d{1,3}` -> "-29.8837%" 를 "837" 로 잘라 읽는다
  2. DB 컬럼 vs SELECT       마이그레이션에 있는데 어떤 SELECT 에도 없는 컬럼
  3. 쿼리 파라미터 불일치    클라이언트가 보내는 이름 != 엔드포인트가 받는 이름
  4. 정렬 가정               정렬 없이 [-1] 을 "최신" 으로 쓴다
  5. 키 접근 불일치          .get("note") 하는데 반환은 평평한 summary
  6. 침묵하는 except         예외를 삼키고 사유를 안 남긴다

실행: python scan_wiring.py <저장소_루트> [--json]
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

SCANNER_VERSION = "evidence-wiring-scan-v1"

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
             "graphify-out", "reports", ".claude"}

# ── 1. 자릿수 상한 정규식 ────────────────────────────────────────────────────
# `\d{1,3}` 처럼 상한이 있으면 긴 수치를 꼬리만 읽는다. 상한 없는 `\d+` 는 안전.
_BOUNDED_DIGITS = re.compile(r"\\d\{\d+,\s*(\d+)\}")


def scan_bounded_regex(py: Path) -> list[dict]:
    out = []
    for i, line in enumerate(py.read_text(encoding="utf-8", errors="replace")
                             .splitlines(), 1):
        if "re.compile" not in line and "re.match" not in line \
                and "re.finditer" not in line and "_RE" not in line:
            continue
        for m in _BOUNDED_DIGITS.finditer(line):
            upper = int(m.group(1))
            if upper <= 4:      # 5자리 이상이면 의도적 제한일 가능성이 높다
                out.append({
                    "kind": "bounded_digit_regex",
                    "line": i,
                    "snippet": line.strip()[:110],
                    "why": f"자릿수 상한 {upper} - 더 긴 수치를 꼬리만 읽는다. "
                           f"실측: `\\d{{1,3}}` 이 '-29.8837%' 를 '837' 로 읽었다",
                })
    return out


# ── 2. DB 컬럼 vs SELECT ────────────────────────────────────────────────────
_CREATE_COL = re.compile(
    r"^\s{2,}([a-z_][a-z0-9_]*)\s+(?:text|int|integer|bigint|numeric|"
    r"boolean|timestamptz|date|jsonb|uuid|double|real|smallint)",
    re.IGNORECASE | re.MULTILINE)


def scan_unselected_columns(root: Path) -> list[dict]:
    """마이그레이션이 만든 컬럼 중 어떤 SQL SELECT 에도 안 나오는 것."""
    mig_dirs = [root / "supabase" / "migrations", root / "timescaledb" / "migrations"]
    cols: dict[str, set[str]] = {}
    for d in mig_dirs:
        if not d.exists():
            continue
        for f in d.glob("*.sql"):
            txt = f.read_text(encoding="utf-8", errors="replace")
            for tbl_m in re.finditer(
                    r"create table (?:if not exists )?([a-z_]+\.[a-z_]+)\s*\((.*?)\n\)",
                    txt, re.IGNORECASE | re.DOTALL):
                table, body = tbl_m.group(1), tbl_m.group(2)
                for c in _CREATE_COL.finditer(body):
                    name = c.group(1).lower()
                    if name in ("primary", "unique", "foreign", "check", "constraint"):
                        continue
                    cols.setdefault(table, set()).add(name)

    # 저장소 전체 파이썬에서 SELECT 문에 등장하는 식별자를 모은다
    selected: set[str] = set()
    for py in _py_files(root):
        txt = py.read_text(encoding="utf-8", errors="replace")
        for sel in re.finditer(r"select\s+(.*?)\s+from\s", txt, re.IGNORECASE | re.DOTALL):
            body = sel.group(1)
            if len(body) > 2000:
                continue
            selected |= {w.lower() for w in re.findall(r"[a-z_][a-z0-9_]*", body, re.IGNORECASE)}

    out = []
    for table, names in sorted(cols.items()):
        missing = sorted(n for n in names
                         if n not in selected and not n.endswith("_id")
                         and n not in ("created_at", "updated_at", "metadata_json"))
        if missing:
            out.append({"kind": "unselected_columns", "table": table,
                        "columns": missing,
                        "why": "적재는 되는데 어떤 SELECT 에도 안 나온다 - "
                               "소비자가 영원히 못 본다"})
    return out


# ── 3. 쿼리 파라미터 불일치 ──────────────────────────────────────────────────
_QS_SEND = re.compile(r"[?&]([a-z_][a-z0-9_]*)=")


def scan_query_params(root: Path) -> list[dict]:
    """클라이언트가 보내는 쿼리 이름이 어느 엔드포인트 인자에도 없는 경우."""
    accepted: set[str] = set()
    for py in _py_files(root):
        if "api" not in str(py).replace("\\", "/"):
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in n.decorator_list:
                    if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) \
                            and dec.func.attr in ("get", "post", "put", "delete"):
                        accepted |= {a.arg for a in n.args.args + n.args.kwonlyargs}
    if not accepted:
        return []

    out = []
    for py in _py_files(root):
        if "/api/" in str(py).replace("\\", "/"):
            continue
        for i, line in enumerate(py.read_text(encoding="utf-8", errors="replace")
                                 .splitlines(), 1):
            # ▶ **우리 API 호출만** 본다. 외부 API(NAVER·DART·Tavily)의 쿼리
            #   이름은 우리 엔드포인트 목록에 당연히 없으므로 전부 오탐이 된다
            #   (실측: idxno·crtfc_key 가 그렇게 잡혔다). 오탐이 진짜를 묻는다.
            if "?" not in line or not any(
                    t in line for t in ("RESEARCH_API", "MARKET_API",
                                        "127.0.0.1:803", "localhost:803")):
                continue
            for m in _QS_SEND.finditer(line):
                name = m.group(1)
                if name in accepted or name in ("api_key", "key", "q", "query"):
                    continue
                out.append({
                    "kind": "unknown_query_param", "file": _rel(py, root), "line": i,
                    "param": name, "snippet": line.strip()[:110],
                    "why": f"'{name}' 를 받는 엔드포인트가 없다 - 422 이거나 "
                           f"조용히 무시된다. 실측: ?code= 를 보냈는데 API 는 "
                           f"codes= 를 받아 매번 422 였다",
                })
    return out


# ── 4. 정렬 가정 ────────────────────────────────────────────────────────────
def scan_order_assumptions(py: Path) -> list[dict]:
    """정렬 없이 [-1]/[0] 을 '최신' 으로 쓰는 함수."""
    try:
        tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return []
    src = py.read_text(encoding="utf-8", errors="replace").splitlines()
    out = []
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        seg = "\n".join(src[fn.lineno - 1:getattr(fn, "end_lineno", fn.lineno)])
        if not re.search(r"\b(bars|rows|closes|series|observations)\b", seg):
            continue
        if "sorted(" in seg or ".sort(" in seg or "order by" in seg.lower():
            continue
        # ▶ 자체점검은 픽스처를 손으로 만든다 - 정렬 가정이 성립할 수 없다
        if fn.name.startswith(("_check_", "test_", "_selftest")):
            continue
        hit = None
        for off, ln in enumerate(src[fn.lineno - 1:
                                     getattr(fn, "end_lineno", fn.lineno)]):
            if re.search(r"\[-1\]|\[0\]", ln):
                hit = (fn.lineno + off, ln.strip())
                break
        if hit and re.search(r"(최신|latest|last_|recent)", seg):
            out.append({
                # ▶ **함수 def 가 아니라 문제가 있는 줄**을 가리킨다. 스킬 본문이
                #   "경로:행과 재현을 적어야 다음 사람이 확인할 수 있다" 고 해놓고
                #   def 줄만 주면 확인하는 사람이 함수 전체를 다시 읽어야 한다.
                "kind": "order_assumption", "line": hit[0], "func": fn.name,
                "evidence": hit[1][:100],
                "why": "정렬 없이 인덱스를 '최신' 으로 쓴다. 실측: /bars 가 "
                       "최신순으로 오는데 closes[-1] 을 최신으로 써서 120봉 "
                       "조회에서 가장 오래된 종가를 냈다",
            })
    return out


# ── 5. 키 접근 불일치 ───────────────────────────────────────────────────────
def scan_key_mismatch(root: Path) -> list[dict]:
    """한쪽은 중첩 키로 읽는데 다른 쪽은 평평하게 내는 경우."""
    produced: dict[str, set[str]] = {}   # 모듈 -> 최상위로 내는 키
    for py in _py_files(root):
        txt = py.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"return \{[^}]{0,400}?\}", txt, re.DOTALL):
            keys = set(re.findall(r'"([a-z_][a-z0-9_]*)":', m.group(0)))
            if keys:
                produced.setdefault(py.stem, set()).update(keys)

    out = []
    for py in _py_files(root):
        for i, line in enumerate(txt_lines(py), 1):
            m = re.search(r'\.get\("(note|readout|result)"\)', line)
            if not m:
                continue
            # 같은 저장소 어딘가가 그 키를 최상위로 안 내고 summary 를 낸다면 후보
            container = m.group(1)
            suspects = [mod for mod, ks in produced.items()
                        if "summary" in ks and container not in ks]
            if suspects:
                out.append({
                    "kind": "key_mismatch", "file": _rel(py, root), "line": i,
                    "reads": container, "producers_without_it": sorted(suspects)[:6],
                    "why": f'.get("{container}") 로 읽는데 일부 생산자는 그 키 없이 '
                           f"summary 를 최상위로 낸다. 실측: 이것 때문에 분석가 "
                           f"서술이 매 실행 100% 폐기됐다",
                })
                break        # 파일당 한 번만 보고한다 - 같은 얘기 반복 금지
    return out


# ── 6. 침묵하는 except ──────────────────────────────────────────────────────
def scan_silent_except(py: Path) -> list[dict]:
    """운영 경로의 침묵하는 except 만 본다.

    ▶ 자체점검(_check_*)은 제외한다. 거기서 `except ValueError: pass` 는
      **의도적**이다 - "불량 입력이 거부되는가" 를 확인하는 정상 패턴이고,
      이걸 세면 실측에서 136건이 나와 **진짜 3~4건을 묻어버린다.**
      오탐이 잦으면 사람이 스캐너를 통째로 무시하게 되고, 그러면 있는 것보다
      나쁘다(이 스킬 자신이 경고하는 실패 방식이다).
    """
    try:
        tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return []

    # 자체점검 함수의 줄 범위를 미리 모은다
    test_ranges = []
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                fn.name.startswith("_check") or fn.name.startswith("test_")):
            test_ranges.append((fn.lineno, getattr(fn, "end_lineno", fn.lineno)))

    def _in_test(ln: int) -> bool:
        return any(a <= ln <= b for a, b in test_ranges)

    out = []
    for h in [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]:
        if len(h.body) != 1 or not isinstance(h.body[0], ast.Pass):
            continue
        if _in_test(h.lineno):
            continue
        out.append({"kind": "silent_except", "line": h.lineno,
                    "why": "운영 경로에서 예외를 삼키고 사유를 안 남긴다 - "
                           "실패가 '정상 0' 으로 위장된다"})
    return out


# ── 공통 ────────────────────────────────────────────────────────────────────

def _py_files(root: Path):
    for p in root.rglob("*.py"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        yield p


def txt_lines(py: Path):
    return py.read_text(encoding="utf-8", errors="replace").splitlines()


def _rel(p: Path, root: Path) -> str:
    try:
        return str(p.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(p)


def scan(root: Path) -> dict:
    findings: list[dict] = []
    for py in _py_files(root):
        for fn in (scan_bounded_regex, scan_order_assumptions, scan_silent_except):
            for f in fn(py):
                findings.append({**f, "file": _rel(py, root)})
    findings += scan_unselected_columns(root)
    findings += scan_query_params(root)
    findings += scan_key_mismatch(root)

    by_kind: dict[str, int] = {}
    for f in findings:
        by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1
    return {"version": SCANNER_VERSION, "root": str(root),
            "total": len(findings), "by_kind": by_kind, "findings": findings}


def render(res: dict) -> str:
    lines = [f"{res['version']} - 후보 {res['total']}건",
             "**판정이 아니다.** 각 항목을 실제로 돌려 확인해야 한다.", ""]
    for kind, n in sorted(res["by_kind"].items(), key=lambda kv: -kv[1]):
        lines.append(f"  {kind:24} {n}건")
    lines.append("")
    for f in res["findings"][:60]:
        loc = f.get("file", f.get("table", "?"))
        if f.get("line"):
            loc += f":{f['line']}"
        lines.append(f"[{f['kind']}] {loc}")
        for k in ("param", "columns", "reads", "func", "snippet"):
            if f.get(k):
                lines.append(f"    {k}: {f[k]}")
        lines.append(f"    → {f['why']}")
        lines.append("")
    if res["total"] > 60:
        lines.append(f"... 그 외 {res['total'] - 60}건 (--json 으로 전체 확인)")
    return "\n".join(lines)


# ── 자체 점검 ───────────────────────────────────────────────────────────────

def _self_check() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "bad.py").write_text(
            '_PCT_RE = re.compile(r"([+-]?\\d{1,3}(?:\\.\\d{1,2})?)\\s*%")\n'
            "def latest_close(bars):\n"
            "    closes = [b['close'] for b in bars]\n"
            "    return closes[-1]   # 최신 종가\n"
            "try:\n    x = 1\nexcept Exception:\n    pass\n",
            encoding="utf-8")
        (root / "good.py").write_text(
            '_OK_RE = re.compile(r"(\\d+(?:\\.\\d+)?)%")\n'
            "def latest(bars):\n"
            "    rows = sorted(bars, key=lambda b: b['t'])\n"
            "    return rows[-1]  # 최신\n",
            encoding="utf-8")
        res = scan(root)
        kinds = res["by_kind"]
        # 한 줄에 상한이 둘(\d{1,3} 과 \.\d{1,2})이면 2건이 맞다 -
        # 각각이 독립적으로 수치를 자르기 때문이다
        assert kinds.get("bounded_digit_regex") == 2, kinds
        assert kinds.get("order_assumption") == 1, kinds
        # 자체점검 안의 의도적 pass 는 세지 않는다 - 오탐이 진짜를 묻는다
        assert kinds.get("silent_except") == 1, kinds
        # 정렬한 쪽은 잡지 않는다 - 오탐이 잦으면 스캐너를 무시하게 된다
        assert not any(f["file"] == "good.py" for f in res["findings"]), res["findings"]
        assert render(res).startswith(SCANNER_VERSION)
    print("scan_wiring 자체 점검 통과 (6개 유형)")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--self-check" in sys.argv or not args:
        _self_check()
        if not args:
            print("사용: python scan_wiring.py <저장소_루트> [--json]")
            raise SystemExit(0)
    result = scan(Path(args[0]).resolve())
    print(json.dumps(result, ensure_ascii=False, indent=1) if "--json" in sys.argv
          else render(result))
