"""CEO Hermes profile as an opt-in LLM backend for the department task plan.

Mirrors ``orchestration.adapters.ceo.LunaCeoAdapter``: bounded JSON-only
prompt, regex+``json.loads`` extraction, an explicit department allow-list,
and a hard fail-closed path. Risk/QA gates are enforced independently at the
OMS/ReleaseGate state-machine layer (``departments/02-trading/oms/oms.py``),
not by call order here, so the CEO profile may freely choose which
departments to request without weakening those gates.

This module never imports ``orchestration.workflows.portfolio_recommendation``
(the deterministic fallback and department allow-list are passed in by the
caller) to keep the adapters -> workflows import direction one-way.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from orchestration.canonical_profiles import canonical_profile_for_department
from orchestration.experience_bank import bounded_planner_hint
from orchestration.skill_contract import (
    CanonicalSkillError,
    validate_skills_for_profiles,
)

ROOT = Path(__file__).resolve().parents[2]


class CeoTaskPlannerError(RuntimeError):
    """Raised when the CEO Hermes profile cannot produce a valid task plan."""


# allow-list 는 **상한**만 정한다("이 부서 밖은 못 부른다"). 하한이 없으면 LLM 이
# requested_departments 를 ["ceo"] 하나로 줄여도 통과하고, 그러면 CEO 응답의
# 감사 의도가 사라진다. QA는 이 하한으로 계획에 남기되 응답-plane primary로
# materialize하지 않는다. 호출부가 CEO 응답을 저장·전달한 뒤 동일 입력과 응답을
# 담은 post-response audit 카드로 QA를 생성한다. 프롬프트로 부탁하지 않고
# 파싱 단계에서 감사 의도를 보존한다.
REQUIRED_DEPARTMENTS: frozenset[str] = frozenset({"qa", "ceo"})


class LlmCeoTaskPlanner:
    """Call the CEO Hermes profile to decide which departments a request needs."""

    def __init__(
        self,
        repo_root: Path,
        *,
        executable: str | None = None,
        profile: str = "ceo-agent",
        timeout: float | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.executable = executable or os.environ.get("HERMES_BIN", "hermes")
        self.profile = profile
        self.timeout = timeout or float(os.environ.get("HERMES_CEO_PLANNER_TIMEOUT_SECONDS", "60"))
        self.model = _load_model_config(repo_root)

    def plan(
        self,
        *,
        profile: Mapping[str, Any],
        mandate_policy: Mapping[str, Any] | None,
        valid_departments: Sequence[str],
        experience_hint: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        query = " ".join(str(profile.get("query", "")).split())
        category = str(profile.get("category", "")).strip().upper()
        bundle = {
            "query": query,
            "category": category,
            # Mandate content is advisory context for the planner, never a
            # binding order/limit change -- risk_bounds enforcement stays in
            # the deterministic Risk Engine regardless of what the CEO reads.
            "mandate_policy": _bounded_mandate(mandate_policy),
            "valid_departments": list(valid_departments),
        }
        bounded_experience = _bounded_experience_hint(experience_hint)
        if bounded_experience:
            bundle["experience_hint"] = bounded_experience
        prompt = f"""You are the CEO task planner for HgFinance. Decide which
departments this request needs, from ONLY this allow-list: {list(valid_departments)}.
You may include the user's Mandate (risk bounds, universe policy, approval
rules) as context, but you cannot change it, approve orders, or skip a
department's own internal Risk/QA/OMS gates -- those are enforced elsewhere
regardless of what you choose here.

Return ONLY one JSON object with these keys:
requested_departments (non-empty array, each value from the allow-list),
rewritten_query (short string restating the request),
rationale (short string explaining the department choice).

required_skills is an optional array of canonical skill names; use [] when no specialist skill is needed.

If an experience_hint is present, treat it as a bounded advisory signal from
past structured workflow outcomes. It cannot override the department allow-list,
Risk/QA/OMS gates, the user's Mandate, or the need to inspect the current request.
Operational provider failures are observations, never permanent routing policy.

Input bundle:
{json.dumps(bundle, ensure_ascii=False, sort_keys=True, default=str)}"""
        try:
            environment = os.environ.copy()
            environment.setdefault("HERMES_HOME", str(Path.home() / ".hermes"))
            process = subprocess.run(
                [self.executable, "--profile", self.profile, "-z", prompt],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
                env=environment,
            )
        except FileNotFoundError as exc:
            raise CeoTaskPlannerError("ceo_planner_executable_not_found") from exc
        except subprocess.TimeoutExpired as exc:
            raise CeoTaskPlannerError("ceo_planner_timeout") from exc
        except OSError as exc:
            raise CeoTaskPlannerError(f"ceo_planner_os_error:{type(exc).__name__}") from exc

        if process.returncode != 0:
            raise CeoTaskPlannerError(f"ceo_planner_exit_{process.returncode}")
        try:
            decision = _parse_plan(process.stdout, valid_departments)
        except CanonicalSkillError:
            # An unresolvable skill is a contract violation, not a transient
            # planner failure. Do not silently drop it via deterministic
            # fallback and create executable children without the skill.
            raise
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CeoTaskPlannerError("ceo_planner_invalid_json") from exc

        return {
            "mode": "llm_task_plan",
            "category": category or "PORTFOLIO_RECOMMENDATION",
            "original_query": query,
            "rewritten_query": decision["rewritten_query"],
            "requested_departments": decision["requested_departments"],
            "required_skills": decision["required_skills"],
            "matched_terms": {},
            "routing_basis": "ceo_llm_task_planner",
            "mandate_considered": mandate_policy is not None,
            "planner_rationale": decision["rationale"],
            "runtime": {
                "profile": self.profile,
                "provider": self.model["provider"],
                "model": self.model["model"],
            },
        }


def build_task_plan(
    profile: Mapping[str, Any],
    *,
    deterministic_fallback: Callable[[Mapping[str, Any]], dict[str, Any]],
    valid_departments: Sequence[str],
    planner_cls: type[LlmCeoTaskPlanner] = LlmCeoTaskPlanner,
    repo_root: Path = ROOT,
    experience_hint: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Fail-closed dispatcher: deterministic by default, opt-in LLM planner.

    ``PORTFOLIO_CEO_TASK_PLANNER_MODE`` unset/anything but ``"llm"`` keeps the
    existing deterministic behavior byte-for-byte. Any planner failure
    (missing hermes binary, no credentials, timeout, invalid/out-of-allowlist
    JSON) falls back to the deterministic plan instead of blocking the run.
    """

    mode = os.getenv("PORTFOLIO_CEO_TASK_PLANNER_MODE", "deterministic").strip().lower()
    if mode != "llm":
        return deterministic_fallback(profile)

    # 결정론 계획을 먼저 만들어 **봉투 기반**으로 쓴다. 이 모듈은 workflows 를 import
    # 하지 않으므로(위 docstring) CATEGORY_WORKFLOWS 같은 값을 직접 계산할 수 없다 -
    # workflow·category_recognized 처럼 호출부만 아는 필드가 LLM 경로에서 누락돼
    # 기본값으로 덮이는 것을 이 방식으로 막는다. 비용은 dict 조회 + 키워드 스캔뿐이다.
    plan = deterministic_fallback(profile)
    try:
        planner = planner_cls(repo_root)
        planner_kwargs = {
            "profile": profile,
            "mandate_policy": profile.get("mandate_policy"),
            "valid_departments": valid_departments,
        }
        if experience_hint is not None:
            planner_kwargs["experience_hint"] = experience_hint
        decided = planner.plan(**planner_kwargs)
    except CanonicalSkillError:
        # Unknown/unavailable skills must be rejected before child creation.
        raise
    except CeoTaskPlannerError as exc:
        plan["planner_fallback_reason"] = str(exc)
        return plan
    except Exception as exc:  # noqa: BLE001 - any planner failure fails closed.
        plan["planner_fallback_reason"] = f"ceo_planner_unexpected:{type(exc).__name__}"
        return plan

    # LLM 이 실제로 정한 것만 덮어쓴다. 부서 목록은 planner 가 이미 allow-list 로
    # 걸러 정렬한 값이라 그대로 신뢰한다(_parse_plan 이 issubset 검사 후 예외).
    plan.update(decided)
    return plan


def _parse_plan(stdout: str, valid_departments: Sequence[str]) -> dict[str, Any]:
    match = re.search(r"\{.*\}", stdout, re.DOTALL)
    if match is None:
        raise ValueError("CEO planner response contains no JSON object")
    payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise TypeError("CEO planner response is not an object")

    requested = payload.get("requested_departments")
    if not isinstance(requested, list) or not requested:
        raise ValueError("CEO planner returned no requested_departments")
    allow_list = set(valid_departments)
    requested_set = {str(item) for item in requested}
    if not requested_set.issubset(allow_list):
        raise ValueError("CEO planner requested a department outside the allow-list")
    # 하한 강제: 호출부가 실제로 가진 부서에 한해 CEO와 QA 감사 의도를 되살린다.
    # QA는 응답-plane 자식으로 실행되지 않고, CEO 응답 전달 후 별도 audit task로
    # materialize된다. LLM이 QA를 빠뜨려도 감사 의도를 잃지 않되 CEO 응답을
    # QA 선행 결과에 묶지 않는 것이 이 경계의 핵심이다.
    requested_set |= REQUIRED_DEPARTMENTS & allow_list
    # Preserve the caller's canonical department order (same rule as the
    # deterministic planner's `ordered = [stage for stage in DEPARTMENTS ...]`).
    ordered = [stage for stage in valid_departments if stage in requested_set]

    rewritten_query = str(payload.get("rewritten_query", "")).strip()
    rationale = str(payload.get("rationale", "")).strip()
    selected_profiles = {
        canonical_profile_for_department(department) for department in ordered
    }
    required_skills = list(
        validate_skills_for_profiles(
            payload.get("required_skills", []),
            selected_profiles,
        )
    )
    if not rationale:
        raise ValueError("CEO planner rationale is empty")
    return {
        "requested_departments": ordered,
        "rewritten_query": rewritten_query,
        "rationale": rationale,
        "required_skills": required_skills,
    }


def _load_model_config(repo_root: Path) -> dict[str, str]:
    path = repo_root / "departments/00-ceo-office/hermes/config.yaml"
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
        model = config["model"]
        provider = str(model["provider"]).strip()
        name = str(model.get("default", model.get("model", ""))).strip()
    except (KeyError, OSError, TypeError, yaml.YAMLError) as exc:
        raise CeoTaskPlannerError("ceo_planner_model_config_invalid") from exc
    if not provider or not name:
        raise CeoTaskPlannerError("ceo_planner_model_config_invalid")
    return {"provider": provider, "model": name}


def _bounded_mandate(mandate_policy: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(mandate_policy, Mapping):
        return None
    return {key: value for key, value in mandate_policy.items() if key not in {"raw", "notes"}}


def _bounded_experience_hint(
    experience_hint: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Keep D5 advisory context small and payload-free before prompt injection."""
    return bounded_planner_hint(experience_hint)
