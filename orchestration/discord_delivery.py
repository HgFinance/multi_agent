"""Small, fail-closed Discord delivery adapter for CEO synthesis results."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from orchestration.answer_contract import (
    bounded_retrieval_attempt,
    format_bounded_retrieval_attempt,
    strip_bounded_retrieval_attempt,
)
from orchestration.discord_idempotency import (
    DiscordIdempotencyStore,
    IdempotencyStoreUnavailable,
    canonical_discord_dedup_key,
)

logger = logging.getLogger(__name__)

_CORRELATION_RE = re.compile(
    r"(?m)^(?:discord_)?(?P<key>request_id|message_id|guild_id|channel_id|thread_id|session_id)=(?P<value>\S+)\s*$"
)


@dataclass(frozen=True)
class DiscordCorrelation:
    request_id: str | None = None
    message_id: str | None = None
    guild_id: str | None = None
    channel_id: str | None = None
    thread_id: str | None = None
    session_id: str | None = None


def _merge(base: dict[str, str], values: Mapping[str, Any]) -> None:
    aliases = {
        "discord_request_id": "request_id",
        "discord_message_id": "message_id",
        "discord_guild_id": "guild_id",
        "discord_channel_id": "channel_id",
        "discord_thread_id": "thread_id",
        "discord_session_id": "session_id",
    }
    for key, value in values.items():
        normalized = aliases.get(str(key), str(key))
        if (
            normalized
            in {
                "request_id",
                "message_id",
                "guild_id",
                "channel_id",
                "thread_id",
                "session_id",
            }
            and value
        ):
            base.setdefault(normalized, str(value))


def _find_in_mapping(value: Any, result: dict[str, str]) -> None:
    if isinstance(value, Mapping):
        _merge(result, value)
        for key in ("body", "comment", "content"):
            if key in value:
                _find_in_mapping(value[key], result)
        for key in (
            "metadata",
            "workflow_metadata",
            "run_metadata",
            "task_run_metadata",
            "task_run",
            "discord_context",
            "correlation",
            "root_task",
            "root_payload",
            "workflow_root",
        ):
            if key in value:
                _find_in_mapping(value[key], result)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            _find_in_mapping(item, result)
    elif isinstance(value, str):
        for match in _CORRELATION_RE.finditer(value):
            result.setdefault(match.group("key"), match.group("value"))


def correlation_from_task(task: Mapping[str, Any]) -> DiscordCorrelation:
    """Read explicit correlation only from the completed synthesis task/root."""

    values: dict[str, str] = {}
    _find_in_mapping(task, values)
    body = str(task.get("body") or "")
    for match in _CORRELATION_RE.finditer(body):
        values.setdefault(match.group("key"), match.group("value"))
    comments = task.get("comments")
    _find_in_mapping(comments, values)
    return DiscordCorrelation(**values)


def _correlation_from_synthesis(
    synthesis_task: Mapping[str, Any],
    root_task: Mapping[str, Any] | None,
) -> DiscordCorrelation:
    """Prefer synthesis-local fields, then the supervisor's exact root."""

    synthesis = correlation_from_task(synthesis_task)
    if root_task is None:
        return synthesis
    root = correlation_from_task(root_task)
    return DiscordCorrelation(
        request_id=synthesis.request_id or root.request_id,
        message_id=synthesis.message_id or root.message_id,
        guild_id=synthesis.guild_id or root.guild_id,
        channel_id=synthesis.channel_id or root.channel_id,
        thread_id=synthesis.thread_id or root.thread_id,
        session_id=synthesis.session_id or root.session_id,
    )


def correlation_from_tasks(
    source_task: Mapping[str, Any],
    root_task: Mapping[str, Any] | None = None,
) -> DiscordCorrelation:
    """Resolve the same child/root correlation used by Discord delivery."""

    return _correlation_from_synthesis(source_task, root_task)


def _message_id_from_request_id(request_id: str | None) -> str | None:
    if not request_id:
        return None
    value = str(request_id)
    if value.startswith("discord:"):
        tail = value.rsplit(":", 1)[-1]
        return tail or None
    return None


def _token_from_env(env: Mapping[str, str], profile: str) -> str | None:
    """Resolve the Discord identity for the requested Hermes profile.

    A profile-specific token is authoritative. The process-level token is only
    a compatibility fallback for deployments that do not keep per-profile
    Discord credentials. The CEO service also accepts its explicit deployment
    secret so a rotated token is not shadowed by a stale mounted profile file.
    """
    home = Path(env.get("HERMES_HOME", "/opt/data"))

    if profile == "ceo-agent":
        token = env.get("DISCORD_BOT_TOKEN_CEO")
        if token:
            return token.strip()

    profile_env = home / "profiles" / profile / ".env"
    try:
        for line in profile_env.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == "DISCORD_BOT_TOKEN":
                token = value.strip().strip('"').strip("'")
                if token:
                    return token
    except OSError:
        pass

    token = env.get("DISCORD_BOT_TOKEN")
    if token:
        return token.strip()

    global_env = home / ".env"
    try:
        for line in global_env.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == "DISCORD_BOT_TOKEN":
                return value.strip().strip('"').strip("'") or None
    except OSError:
        pass

    return None


class DiscordFinalDelivery:
    """Publish one final CEO answer through Discord's existing bot identity."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        sender: Callable[[str, str, Mapping[str, str]], Mapping[str, Any]]
        | None = None,
        editor: Callable[[str, str, str, Mapping[str, str]], Mapping[str, Any]]
        | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.environment = dict(environment or os.environ)
        self.sender = sender or self._send_http
        self.editor = editor or self._edit_http
        self.timeout = timeout

    def _send_http(
        self,
        channel_id: str,
        payload: str,
        headers: Mapping[str, str],
    ) -> Mapping[str, Any]:
        request = Request(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            data=payload.encode("utf-8"),
            headers=dict(headers),
            method="POST",
        )
        with urlopen(request, timeout=self.timeout) as response:
            body = response.read().decode("utf-8")
        decoded = json.loads(body) if body else {}
        return decoded if isinstance(decoded, Mapping) else {}

    def _edit_http(
        self,
        channel_id: str,
        message_id: str,
        payload: str,
        headers: Mapping[str, str],
    ) -> Mapping[str, Any]:
        request = Request(
            f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}",
            data=payload.encode("utf-8"),
            headers=dict(headers),
            method="PATCH",
        )
        with urlopen(request, timeout=self.timeout) as response:
            body = response.read().decode("utf-8")
        decoded = json.loads(body) if body else {}
        return decoded if isinstance(decoded, Mapping) else {}

    @staticmethod
    def _humanize_content(content: str) -> str:
        """Translate common runtime labels in manager/user-facing messages."""

        replacements = (
            (
                "Review the accounting, liquidity, fee, 순자산 가치, or portfolio-state implications relevant to the request using 읽기 전용 근거 자료. Do not mutate a ledger or confirm 순자산 가치.",
                "회계·유동성·수수료·순자산 가치·포트폴리오 상태를 읽기 전용 근거로 검토합니다. 원장 변경이나 순자산 가치 확정은 하지 않습니다.",
            ),
            (
                "Notion·LangSmith·Discord 전달 상태는 이 Accounting handoff에 확인 가능한 증거가 없어 검증 완료로 주장하지 않습니다.",
                "Notion·LangSmith·Discord 전달 상태는 아래 시스템 전달 상태와 관리자 검증 기록을 기준으로 확인합니다.",
            ),
            (
                "요청에 포함된 Notion·LangSmith·Discord 전달 상태는 이번 Accounting handoff에 검증 근거가 없어 확인 불가합니다.",
                "Notion·LangSmith·Discord 전달 상태는 아래 시스템 전달 상태와 관리자 검증 기록을 기준으로 확인합니다.",
            ),
            ("snapshot_resolvable=false", "현재 투자지침 확인 불가"),
            (
                "source_of_record=accounting.journals (Supabase)",
                "자료 기준: 회계 시스템 원장",
            ),
            ("official 순자산 가치 close pending", "공식 순자산 가치 확정 보류"),
            ("LS broker evidence", "증권사 조회 자료"),
            ("official NAV close pending", "공식 순자산 가치 확정 보류"),
            ("공식 순자산 가치 close", "공식 순자산 가치 확정"),
            ("Accounting / Portfolio", "회계·포트폴리오"),
            ("Accounting/Portfolio", "회계·포트폴리오"),
            ("Portfolio/Accounting", "회계·포트폴리오"),
            ("Accounting Engine", "회계 시스템"),
            ("Strategy", "전략"),
            ("accounting.broker-evidence.v1", "브로커 조회 자료"),
            ("BROKER_POSITION_TR_MISMATCH", "브로커 포지션 수량 대사 차이"),
            ("BROKER_EVIDENCE_INCOMPLETE", "브로커 자료 불완전"),
            ("severity REVIEW", "검토 필요"),
            ("Portfolio Control/대사 담당", "대사 담당"),
            ("전략는", "전략은"),
            ("close readiness", "마감 준비 가능 여부"),
            ("hgfinance.accounting-snapshot.v1", "회계 조회 자료"),
            ("hgfinance.mandate-snapshot.v1", "투자지침 조회 자료"),
            ("quality_status=WARN", "자료 품질: 주의"),
            ("authoritative=false/is_official=false", "공식 확정 자료 아님"),
            ("is_official=false", "공식 확정 자료 아님"),
            ("is_official=true", "공식 확정 자료"),
            ("close 전", "마감 전"),
            ("official NAV close", "공식 순자산 가치 마감"),
            ("공식 NAV close", "공식 순자산 가치 마감"),
            ("official valuation close", "공식 평가 마감"),
            ("close 대사", "마감 대사"),
            ("reconciliation open", "대사 진행 중"),
            ("대사 대사 차이", "대사 차이"),
            ("원장 close", "원장 마감"),
            ("reversing entry", "승인된 정정 전표"),
            ("reversing/additional entry", "승인된 정정·추가 전표"),
            ("instrument mapping", "종목 식별 정보 연결"),
            ("reference mapping", "기준정보 연결"),
            ("unmapped", "미매핑"),
            ("is_공식=false", "공식 확정 자료 아님"),
            ("source_of_record", "자료 기준"),
            ("quality_status", "자료 품질 상태"),
            ("authoritative 계좌", "공식 계좌"),
            ("authoritative 자료", "공식 확정 자료"),
            ("instrument_id", "종목 식별자"),
            ("accounting.journals (Supabase)", "회계 시스템 원장"),
            ("accounting.journals", "회계 시스템 원장"),
            ("accounting.journals, Supabase", "회계 시스템 원장"),
            ("/stock/accno", "증권사 잔고 조회"),
            ("LS OPEN API", "증권사 조회 API"),
            ("LS ", "증권사 "),
            ("LS상", "증권사 자료상"),
            ("LS의", "증권사 자료의"),
            ("Gross/Net exposure", "총·순 익스포저"),
            ("Gross Exposure", "총 익스포저"),
            ("gross exposure", "총 익스포저"),
            ("net exposure", "순 익스포저"),
            ("open reconciliation breaks", "미해결 대사 차이"),
            ("open Break", "미해결 대사 차이"),
            ("OPEN BREAKS", "미해결 대사 차이"),
            ("position reconciliation", "포지션 대사"),
            ("comparison_basis", "대사 비교 기준"),
            ("trade_basis_quantity", "매매기준 보유수량"),
            (
                "CSPAQ12300.BnsBaseBalQty vs t0424.janqty",
                "증권사 매매기준 보유수량과 체결기준 잔고수량",
            ),
            ("UNSUPPORTED_IN_PAPER", "PAPER에서 제공되지 않음"),
            ("NO_ACTIVITY", "해당 기간 거래·주문 없음"),
            ("expected=true", "예상 가능한 상태: 예"),
            ("projection", "조회 자료"),
            ("posted journal", "게시 원장"),
            ("account-wide", "계좌 전체 기준"),
            ("coverage", "자료 확인 범위"),
            ("complete", "완료"),
            ("quality가", "자료 품질은"),
            ("instrument", "종목"),
            ("gross", "총 익스포저"),
            ("mandate", "투자지침"),
            ("승인된 승인된", "승인된"),
            ("schema_version", "자료 형식"),
            ("withdrawable", "출금 가능액"),
            ("substitute_orderable", "대체 주문가능액"),
            ("max_difference", "최대 차이"),
            ("NEEDS_PARAMETERS", "필수 조건 미제공"),
            ("collateral shortfall", "담보 부족액"),
            ("BEP", "손익분기 가격"),
            ("evidence", "근거 자료"),
            (" TR", " 조회 항목"),
            ("mark_as_of", "가격 기준 시각"),
            ("mark 시각", "가격 기준 시각"),
            ("mark 기준", "가격 기준"),
            ("as_of", "기준 시각"),
            ("mark_price", "가격"),
            ("CSPAQ12300", "손익분기·잔고 조회"),
            ("CSPAQ12200", "예수금·주문가능액 조회"),
            ("CSPAQ22200", "예수금·주문가능액 보조 조회"),
            ("CSPAQ13700", "주문체결 조회"),
            ("CSPAQ00600", "신용한도 조회"),
            ("CSPBQ00200", "증거금별 주문가능수량 조회"),
            ("CDPCQ04700", "기간 거래내역 조회"),
            ("FOCCQ33600", "기간 수익률 조회"),
            ("t0150", "당일 거래·수수료 조회"),
            ("t0151", "전일 거래·수수료 조회"),
            ("t0424", "잔고 조회"),
            ("t0425", "체결·미체결 조회"),
            ("Fund ", "펀드 "),
            ("Fund:", "펀드:"),
            ("Book ", "장부 "),
            ("Book:", "장부:"),
            ("KRW", "원"),
            ("Engine", "회계 시스템"),
            ("symbol/display_name", "종목 식별 정보"),
            ("CSPAQ12200·CSPAQ22200·CSPAQ12300", "증권사 세 가지 조회 항목"),
            ("Long", "롱"),
            ("Short", "숏"),
            ("PnL", "손익"),
            ("BREAK", "대사 차이"),
            ("Break", "대사 차이"),
            ("leg", "포지션 구간"),
            ("fees", "수수료"),
            ("taxes", "세금"),
            ("cash_orderable", "현금 주문가능액"),
            ("receivable", "미수금"),
            ("deposit", "예수금"),
            ("cross-check", "교차 확인"),
            ("세 TR 간", "세 조회 항목 간"),
            ("Preliminary", "예비"),
            ("PRELIMINARY", "예비"),
            ("close-ready", "마감 확인 가능"),
            ("valuation close", "평가 마감"),
            ("confirmed", "확인된"),
            ("timing", "기준 시각"),
            ("advisory", "검토용"),
            ("snapshot", "조회 자료"),
            ("official", "공식"),
            ("Sources:", "출처:"),
            ("weight", "비중"),
            ("close pending", "확정 보류"),
            ("block_reason", "판단 보류 사유"),
            ("Mandate가", "투자지침이"),
            ("Mandate를", "투자지침을"),
            ("Mandate와", "투자지침과"),
            ("Mandate의", "투자지침의"),
            ("Mandate", "투자지침"),
            ("MODERATE", "보통"),
            ("위반 없음(no_breach)", "현재 입력만으로 위반을 확인하지 못함"),
            ("no_breach", "현재 입력만으로 위반을 확인하지 못함"),
            ("확인된 위반 없음", "현재 입력만으로 위반을 확인하지 못함"),
            ("**risk**", "**리스크 부서**"),
            ("Risk 부서", "리스크 부서"),
            ("HIGH 차단으로", "중요 차단 사유로"),
            ("HIGH 차단", "중요 차단 사유"),
            ("Research·Trading·Risk", "리서치·트레이딩·리스크"),
            ("Research", "리서치"),
            ("Trading", "트레이딩"),
            ("**research**", "**리서치 부서**"),
            ("**accounting**", "**회계·포트폴리오 부서**"),
            ("DEFER", "판단 보류"),
            ("authoritative=false", "공식 확정 자료 아님"),
            ("live_order_submission_allowed=false", "실제 주문 제출 허용 안 됨"),
            ("unavailable_reference_mapping", "섹터 매핑 사용 불가"),
            ("order_intent_candidate", "주문 후보"),
            ("OrderIntent", "주문 후보"),
            ("REQUIRES_USER_REVIEW", "사용자 확인 필요"),
            ("ELEVATED", "주의 수준 높음"),
            ("Risk/Compliance Gate", "리스크·준법 확인 절차"),
            ("Risk", "리스크"),
            ("PAPER/read-only", "분석용 가상거래·읽기 전용"),
            ("read-only", "읽기 전용"),
            ("WARN", "주의"),
        )
        bounded_record = bounded_retrieval_attempt(str(content or ""))
        rendered = (
            strip_bounded_retrieval_attempt(str(content or ""))
            if bounded_record is not None
            else str(content or "")
        )
        # Some model/provider paths serialize line breaks as the two literal
        # characters ``\\`` and ``n``.  Normalize those at the single outbound
        # boundary so Discord receives readable paragraphs instead of a
        # visible ``\\n`` sequence.  Actual newlines are preserved.
        rendered = (
            rendered.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")
        )
        for internal, friendly in replacements:
            rendered = rendered.replace(internal, friendly)
        if "📒 **회계·포트폴리오 부서**" in rendered and "읽기 전용" not in rendered:
            rendered = rendered.replace(
                "✅ 분석을 완료했습니다.",
                "✅ 분석을 완료했습니다.\n\n범위: 분석용 가상거래·읽기 전용 검토이며 주문·원장 변경·공식 순자산 가치 확정은 수행하지 않았습니다.",
                1,
            )
        rendered = rendered.replace("`", "")
        rendered = re.sub(
            r"(?m)(^.*?)(?:주요 인용|조회 근거):\s*"
            r"(?:ls-tr:[^,\s]+(?:,\s*)?)+",
            r"\1조회 근거: 증권사 조회 기록",
            rendered,
        )
        rendered = re.sub(r"ls-tr:[^,\s)]+", "증권사 조회 기록", rendered)
        rendered = re.sub(
            r"종목 식별자\s+[0-9a-f]{8}-[0-9a-f-]{27,36}",
            "식별 불명 포지션",
            rendered,
            flags=re.IGNORECASE,
        )
        rendered = re.sub(
            r"\b(?:CSPAQ|CSPBQ|CDPCQ|FOCCQ)[A-Za-z0-9]+\b|\bt\d{4}\b",
            "증권사 조회 항목",
            rendered,
        )
        # NAV 만 정규식이다 - str.replace 는 부분 문자열도 바꿔서 UNAVAILABLE 이
        # "U순자산 가치AILABLE" 로 깨졌다(2026-08-26 HR 유휴 리포트 실측). `\b` 는
        # 한글이 \w 라 "NAV가" 를 놓치므로 ASCII 문자만 배제한다.
        rendered = re.sub(r"(?<![A-Za-z])NAV(?![A-Za-z])", "순자산 가치", rendered)
        rendered = re.sub(
            r"(?:PAPER(?: 가상거래)? 기준 |PAPER만으로는 )?"
            r"현재 입력만으로 위반을 확인하지 못함으로 "
            r"(?:보았|회신되었)지만",
            "법률 위반 여부를 확정할 수 없으며",
            rendered,
        )
        rendered = rendered.replace("PAPER", "분석용 가상거래")
        rendered = rendered.replace("대사 대사 차이", "대사 차이")
        rendered = rendered.replace("자료 조회 자료", "조회 자료")
        rendered = rendered.replace(
            "공식 확정 자료 아님이고", "공식 확정 자료가 아니며"
        )
        rendered = rendered.replace("자료과", "자료와")
        rendered = rendered.replace("주의과", "주의와")
        rendered = rendered.replace("확정로", "확정으로")
        rendered = rendered.replace("ERROR이고", "조회 오류이며")
        rendered = rendered.replace("EMPTY입니다", "조회 결과가 없습니다")
        rendered = rendered.replace(
            "공식 확정 자료 아님, 공식 확정 자료 아님", "공식 확정 자료 아님"
        )
        if "📒 **회계·포트폴리오 부서**" in rendered and len(rendered) > 1700:
            rendered = DiscordFinalDelivery._compact_accounting_content(rendered)
        if bounded_record is not None:
            rendered = (
                f"{rendered.rstrip()}\n\n"
                f"{format_bounded_retrieval_attempt(bounded_record)}"
            ).strip()
        return rendered

    @staticmethod
    def _compact_accounting_content(rendered: str) -> str:
        """Keep accounting department cards readable while preserving evidence."""

        lines = [line.strip() for line in rendered.splitlines() if line.strip()]

        def first(*terms: str) -> str:
            for line in lines:
                if any(term in line for term in terms):
                    return line
            return ""

        def first_field(label: str) -> str:
            for line in lines:
                candidate = line.removeprefix("-").strip()
                if candidate.startswith(label):
                    return candidate
            return ""

        metrics: list[str] = []
        for line in lines:
            if line.startswith("-") and any(
                term in line
                for term in (
                    "순자산 가치:",
                    "NAV:",
                    "현금:",
                    "증권가치:",
                    "증권 평가액:",
                    "실현손익:",
                    "미실현손익:",
                    "기록된 수수료:",
                    "기록된 세금:",
                )
            ):
                metrics.append(line)

        warnings: list[str] = []
        for line in lines:
            if (
                line.startswith("-")
                and any(
                    term in line
                    for term in (
                        "대사 차이",
                        "섹터 매핑",
                        "식별자",
                        "공식 확정 자료 아님",
                    )
                )
                and line not in warnings
            ):
                warnings.append(line)
            if len(warnings) >= 4:
                break

        breaks = [
            line
            for line in lines
            if "차이" in line
            and "주" in line
            and " vs " in line
            and line.startswith("-")
        ][:5]
        conclusion = first("결론 및 조치", "### 결론")
        output = [
            "📒 **회계·포트폴리오 부서**",
            "✅ 분석을 완료했습니다.",
            (first_field("범위:") or "범위: 분석용 가상거래·읽기 전용 검토")[:360],
            (first_field("상태:") or "상태: 예비·주의 — 공식 수치 확정 보류")[:360],
            "",
            "### 핵심 수치",
            *(metrics[:8] or ["- 제공된 핵심 수치 확인 필요"]),
            "",
            "### 확인된 주의사항",
            *(warnings or ["- 대사 및 자료 품질 확인 필요"]),
        ]
        if breaks:
            output.extend(["", "### 미해결 대사 차이", *breaks])
        if conclusion and not conclusion.startswith("PAPER 경계"):
            output.extend(["", "### 결론", conclusion[:620]])
        elif not conclusion:
            output.extend(
                [
                    "",
                    "### 결론",
                    "- 공식 수치 확정은 보류하며, 미해결 대사 차이를 해소한 뒤 재검증이 필요합니다.",
                ]
            )
        output.extend(
            [
                "",
                "> 분석용 가상거래·읽기 전용 검토입니다. 주문·원장 변경·공식 순자산 가치 확정은 수행하지 않았습니다.",
            ]
        )
        return "\n".join(output)

    @staticmethod
    def _detail_chunks(content: str, limit: int = 1700) -> tuple[str, ...]:
        """Split long department detail safely below Discord's message limit."""
        remaining = str(content or "").strip()
        if not remaining:
            return ()

        chunks: list[str] = []

        while len(remaining) > limit:
            cut = remaining.rfind("\n", 0, limit + 1)
            if cut < max(200, limit // 2):
                cut = remaining.rfind(" ", 0, limit + 1)
            if cut < max(200, limit // 2):
                cut = limit

            chunk = remaining[:cut].strip()
            if chunk:
                chunks.append(chunk)

            remaining = remaining[cut:].lstrip()

        if remaining:
            chunks.append(remaining)

        return tuple(chunks)

    @staticmethod
    def _response_message_id(response: Any) -> str | None:
        """Accept a Discord send only when it returns a durable message id."""

        if not isinstance(response, Mapping):
            return None
        message_id = str(response.get("id") or "").strip()
        return message_id or None

    def upsert_thread_card(
        self,
        *,
        root_task_id: str,
        source_task: Mapping[str, Any],
        root_task: Mapping[str, Any] | None,
        content: str,
        store: DiscordIdempotencyStore,
        profile: str,
        response_key_suffix: str,
        update_existing: bool = True,
    ) -> str:
        """Create one request-thread card, then optionally edit that same message."""

        correlation = _correlation_from_synthesis(source_task, root_task)

        source_message_id = correlation.message_id or _message_id_from_request_id(
            correlation.request_id
        )

        thread_id = correlation.thread_id

        if not thread_id:
            context: Mapping[str, str | None] = {}
            inbound_key = None

            if source_message_id:
                inbound_key = store.inbound_key_for_message(
                    str(source_message_id),
                    "ceo-agent",
                )

            if not inbound_key and correlation.session_id:
                inbound_key = store.inbound_key_for_session(
                    str(correlation.session_id),
                    "ceo-agent",
                )

            if inbound_key:
                context = store.inbound_context(
                    inbound_key,
                    "ceo-agent",
                )

            thread_id = context.get("thread_id") or source_message_id

        if not thread_id:
            logger.info(
                "discord-thread-card root=%s profile=%s status=missing_thread",
                root_task_id,
                profile,
            )
            return "missing_thread"

        token = _token_from_env(self.environment, profile)
        if not token:
            logger.error(
                "discord-thread-card root=%s profile=%s "
                "status=failed error=credential_unavailable",
                root_task_id,
                profile,
            )
            return "failed"

        # Keep one department card compact enough for a single Discord message.
        rendered = self._humanize_content(content).strip()
        if not rendered:
            return "empty"

        if len(rendered) > 1900:
            rendered = rendered[:1897].rstrip() + "..."

        guild_id = correlation.guild_id or "unknown"
        correlation_message_id = source_message_id or root_task_id

        dedup_key = canonical_discord_dedup_key(
            guild_id,
            str(thread_id),
            str(correlation_message_id),
        )

        safe_suffix = re.sub(
            r"[^A-Za-z0-9_.:-]+",
            "-",
            str(response_key_suffix or "thread-card"),
        )[:150]

        response_key = f"{dedup_key}:{safe_suffix}"
        content_hash = hashlib.sha256(rendered.encode("utf-8")).hexdigest()

        headers = {
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "HgFinance-DiscordDelivery/2.6",
        }

        payload = json.dumps(
            {"content": rendered},
            ensure_ascii=False,
        )

        def update_existing_card(message_id: str, operation: str) -> str:
            try:
                if store.outbound_content_hash(response_key, profile) == content_hash:
                    logger.info(
                        "discord-thread-card root=%s profile=%s "
                        "thread_id=%s message_id=%s status=unchanged",
                        root_task_id,
                        profile,
                        thread_id,
                        message_id,
                    )
                    return "unchanged"
            except IdempotencyStoreUnavailable:
                logger.exception(
                    "discord-thread-card root=%s profile=%s "
                    "status=failed error=ledger_unavailable",
                    root_task_id,
                    profile,
                )
                return "failed"

            try:
                self.editor(
                    str(thread_id),
                    str(message_id),
                    payload,
                    headers,
                )
            except (HTTPError, URLError, OSError, TimeoutError, ValueError):
                logger.exception(
                    "discord-thread-card root=%s profile=%s status=failed operation=%s",
                    root_task_id,
                    profile,
                    operation,
                )
                return "failed"

            try:
                store.mark_outbound(
                    response_key,
                    "COMPLETED",
                    profile,
                    content_hash=content_hash,
                )
            except IdempotencyStoreUnavailable:
                logger.exception(
                    "discord-thread-card root=%s profile=%s "
                    "status=failed error=ledger_unavailable_after_patch",
                    root_task_id,
                    profile,
                )
                return "failed"

            logger.info(
                "discord-thread-card root=%s profile=%s "
                "thread_id=%s message_id=%s status=updated",
                root_task_id,
                profile,
                thread_id,
                message_id,
            )
            return "updated"

        try:
            existing_message_id = store.outbound_message_id(
                response_key,
                profile,
            )
        except IdempotencyStoreUnavailable:
            logger.exception(
                "discord-thread-card root=%s profile=%s "
                "status=failed error=ledger_unavailable",
                root_task_id,
                profile,
            )
            return "failed"

        if existing_message_id:
            if not update_existing:
                logger.info(
                    "discord-thread-card root=%s profile=%s "
                    "thread_id=%s message_id=%s status=unchanged",
                    root_task_id,
                    profile,
                    thread_id,
                    existing_message_id,
                )
                return "unchanged"

            return update_existing_card(existing_message_id, "patch")

        try:
            claim = store.claim_outbound(
                response_key=response_key,
                dedup_key=dedup_key,
                profile=profile,
            )
        except IdempotencyStoreUnavailable:
            logger.exception(
                "discord-thread-card root=%s profile=%s "
                "status=failed error=ledger_unavailable",
                root_task_id,
                profile,
            )
            return "failed"

        if not claim.admitted:
            try:
                existing_message_id = store.outbound_message_id(
                    response_key,
                    profile,
                )
            except IdempotencyStoreUnavailable:
                logger.exception(
                    "discord-thread-card root=%s profile=%s "
                    "status=failed error=ledger_unavailable",
                    root_task_id,
                    profile,
                )
                return "failed"

            if existing_message_id:
                return update_existing_card(existing_message_id, "patch-after-dedup")

            return "deduped"

        try:
            response = self.sender(
                str(thread_id),
                payload,
                headers,
            )
        except (HTTPError, URLError, OSError, TimeoutError, ValueError):
            store.mark_outbound(response_key, "FAILED", profile)
            logger.exception(
                "discord-thread-card root=%s profile=%s status=failed operation=post",
                root_task_id,
                profile,
            )
            return "failed"

        response_message_id = self._response_message_id(response)
        if response_message_id is None:
            store.mark_outbound(response_key, "FAILED", profile)
            logger.error(
                "discord-thread-card root=%s profile=%s status=failed "
                "error=missing_message_id",
                root_task_id,
                profile,
            )
            return "failed"

        store.mark_outbound(
            response_key,
            "COMPLETED",
            profile,
            response_message_id,
            content_hash=content_hash,
        )

        logger.info(
            "discord-thread-card root=%s profile=%s "
            "thread_id=%s message_id=%s status=created",
            root_task_id,
            profile,
            thread_id,
            response_message_id,
        )
        return "created"

    def deliver_to_existing_thread(
        self,
        *,
        root_task_id: str,
        source_task: Mapping[str, Any],
        root_task: Mapping[str, Any] | None = None,
        content: str,
        title: str,
        store: DiscordIdempotencyStore,
        profile: str,
        response_key_suffix: str,
    ) -> str:
        """Publish full department detail into the request's existing thread."""

        correlation = _correlation_from_synthesis(source_task, root_task)

        message_id = correlation.message_id or _message_id_from_request_id(
            correlation.request_id
        )

        # Resolve the request's EXISTING Discord thread.
        #
        # Resolution precedence:
        #   1. explicit task/root thread_id
        #   2. CEO inbound ledger thread_id
        #   3. Discord starter message id
        #
        # HgFinance's Discord request threads are public threads created from
        # the originating message. Discord uses that starter message id as the
        # resulting thread/channel id.
        thread_id = correlation.thread_id

        if not thread_id:
            context = {}

            inbound_key = None

            if message_id:
                inbound_key = store.inbound_key_for_message(
                    str(message_id),
                    "ceo-agent",
                )

            if not inbound_key and correlation.session_id:
                inbound_key = store.inbound_key_for_session(
                    str(correlation.session_id),
                    "ceo-agent",
                )

            if inbound_key:
                context = store.inbound_context(
                    inbound_key,
                    "ceo-agent",
                )

            thread_id = context.get("thread_id") or message_id

        if not thread_id:
            logger.info(
                "discord-detail-thread root=%s profile=%s "
                "status=missing_thread message_id=%s session_id=%s",
                root_task_id,
                profile,
                message_id or "",
                correlation.session_id or "",
            )
            return "missing_thread"

        token = _token_from_env(self.environment, profile)
        if not token:
            logger.error(
                "discord-detail-thread root=%s profile=%s "
                "status=failed error=credential_unavailable",
                root_task_id,
                profile,
            )
            return "failed"

        chunks = self._detail_chunks(self._humanize_content(content))
        if not chunks:
            return "empty"

        guild_id = correlation.guild_id or "unknown"
        message_id = (
            correlation.message_id
            or _message_id_from_request_id(correlation.request_id)
            or root_task_id
        )

        dedup_key = canonical_discord_dedup_key(
            guild_id,
            str(thread_id),
            str(message_id),
        )

        safe_suffix = re.sub(
            r"[^A-Za-z0-9_.:-]+",
            "-",
            str(response_key_suffix or "detail"),
        )[:150]

        total = len(chunks)

        for index, chunk in enumerate(chunks, start=1):
            response_key = f"{dedup_key}:{safe_suffix}:chunk-{index}-of-{total}"

            try:
                claim = store.claim_outbound(
                    response_key=response_key,
                    dedup_key=dedup_key,
                    profile=profile,
                )
            except IdempotencyStoreUnavailable:
                logger.error(
                    "discord-detail-thread root=%s profile=%s "
                    "status=failed error=ledger_unavailable",
                    root_task_id,
                    profile,
                )
                return "failed"

            if not claim.admitted:
                continue

            if total > 1:
                header = f"**{title} [{index}/{total}]**\n\n"
            else:
                header = f"**{title}**\n\n"

            body = {
                "content": header + chunk,
            }

            try:
                response = self.sender(
                    str(thread_id),
                    json.dumps(body, ensure_ascii=False),
                    {
                        "Authorization": f"Bot {token}",
                        "Content-Type": "application/json",
                        "User-Agent": "HgFinance-DiscordDelivery/2.5",
                    },
                )
            except HTTPError as exc:
                store.mark_outbound(response_key, "FAILED", profile)
                if exc.code == 404 and index == 1:
                    # A request thread can be deleted between ingress and
                    # terminal delivery. Let the supervisor use its existing
                    # parent-channel fallback; never retry a partial
                    # multi-message delivery into a different destination.
                    logger.warning(
                        "discord-detail-thread root=%s profile=%s "
                        "status=missing_thread error=stale_thread",
                        root_task_id,
                        profile,
                    )
                    return "missing_thread"
                logger.exception(
                    "discord-detail-thread root=%s profile=%s "
                    "chunk=%d/%d status=failed",
                    root_task_id,
                    profile,
                    index,
                    total,
                )
                return "failed"
            except (URLError, OSError, TimeoutError, ValueError):
                store.mark_outbound(response_key, "FAILED", profile)
                logger.exception(
                    "discord-detail-thread root=%s profile=%s "
                    "chunk=%d/%d status=failed",
                    root_task_id,
                    profile,
                    index,
                    total,
                )
                return "failed"

            response_message_id = self._response_message_id(response)
            if response_message_id is None:
                store.mark_outbound(response_key, "FAILED", profile)
                logger.error(
                    "discord-detail-thread root=%s profile=%s "
                    "chunk=%d/%d status=failed error=missing_message_id",
                    root_task_id,
                    profile,
                    index,
                    total,
                )
                return "failed"

            store.mark_outbound(
                response_key,
                "COMPLETED",
                profile,
                response_message_id,
            )

        logger.info(
            "discord-detail-thread root=%s profile=%s "
            "thread_id=%s chunks=%d status=sent",
            root_task_id,
            profile,
            thread_id,
            total,
        )
        return "sent"

    def deliver(
        self,
        *,
        root_task_id: str,
        synthesis_task: Mapping[str, Any],
        root_task: Mapping[str, Any] | None = None,
        content: str,
        store: DiscordIdempotencyStore,
        profile: str = "ceo-agent",
        response_key_suffix: str = "final",
    ) -> str:
        correlation = _correlation_from_synthesis(synthesis_task, root_task)
        explicit_message_id = correlation.message_id or _message_id_from_request_id(
            correlation.request_id
        )
        message_id = explicit_message_id
        inbound_key: str | None = None
        context: Mapping[str, str | None] = {}
        correlation_source = "explicit" if explicit_message_id else "missing"
        logger.info(
            "discord-correlation root=%s request_id=%s session_id=%s channel_id=%s message_id=%s",
            root_task_id,
            correlation.request_id or "",
            correlation.session_id or "",
            correlation.channel_id or "",
            message_id or "",
        )
        if not message_id and correlation.session_id:
            inbound_key = store.inbound_key_for_session(correlation.session_id, profile)
            if inbound_key:
                context = store.inbound_context(inbound_key, profile)
                message_id = str(context.get("message_id") or "") or None
                correlation_source = "session_ledger"

        if message_id:
            # Explicit correlation wins. The message ledger is only an exact
            # enrichment lookup, never a recent/global-message fallback.
            message_inbound_key = store.inbound_key_for_message(message_id, profile)
            if message_inbound_key:
                inbound_key = message_inbound_key
                context = store.inbound_context(inbound_key, profile)
                if correlation_source == "missing":
                    correlation_source = "message_ledger"

        if not message_id:
            logger.warning(
                "discord-correlation root=%s source=missing session_id=%s",
                root_task_id,
                correlation.session_id or "",
            )
            logger.warning(
                "discord-final-delivery root=%s status=missing_context",
                root_task_id,
            )
            return "missing_context"
        guild_id = correlation.guild_id or context.get("guild_id") or "unknown"
        channel_id = correlation.channel_id or context.get("channel_id")
        if not channel_id:
            logger.warning(
                "discord-final-delivery root=%s status=missing_context",
                root_task_id,
            )
            return "missing_context"
        logger.info(
            "discord-correlation root=%s source=%s session_id=%s message_id=%s channel_id=%s",
            root_task_id,
            correlation_source,
            correlation.session_id or context.get("session_id") or "",
            message_id,
            channel_id,
        )
        dedup_key = (
            inbound_key
            if inbound_key
            else canonical_discord_dedup_key(guild_id, channel_id, message_id)
        )
        safe_suffix = re.sub(
            r"[^A-Za-z0-9_.:-]+",
            "-",
            str(response_key_suffix or "final"),
        )[:180]
        response_key = f"{dedup_key}:{safe_suffix}"
        try:
            claim = store.claim_outbound(
                response_key=response_key,
                dedup_key=dedup_key,
                profile=profile,
            )
        except IdempotencyStoreUnavailable:
            logger.error(
                "discord-final-delivery root=%s status=failed error=ledger_unavailable",
                root_task_id,
            )
            return "failed"
        if not claim.admitted:
            logger.info(
                "discord-final-delivery root=%s channel_id=%s message_id=%s status=deduped",
                root_task_id,
                channel_id,
                message_id,
            )
            return "deduped"

        token = _token_from_env(self.environment, profile)
        if not token:
            store.mark_outbound(response_key, "FAILED", profile)
            logger.error(
                "discord-final-delivery root=%s status=failed error=credential_unavailable",
                root_task_id,
            )
            return "failed"

        body: dict[str, Any] = {"content": self._humanize_content(content)}
        body["message_reference"] = {
            "message_id": message_id,
            "channel_id": channel_id,
            "fail_if_not_exists": False,
        }
        try:
            response = self.sender(
                str(channel_id),
                json.dumps(body, ensure_ascii=False),
                {
                    "Authorization": f"Bot {token}",
                    "Content-Type": "application/json",
                    "User-Agent": "HgFinance-DiscordDelivery/2.4",
                },
            )
        except (HTTPError, URLError, OSError, TimeoutError, ValueError):
            store.mark_outbound(response_key, "FAILED", profile)
            logger.exception(
                "discord-final-delivery root=%s status=failed",
                root_task_id,
            )
            return "failed"

        response_message_id = self._response_message_id(response)
        if response_message_id is None:
            store.mark_outbound(response_key, "FAILED", profile)
            logger.error(
                "discord-final-delivery root=%s status=failed error=missing_message_id",
                root_task_id,
            )
            return "failed"
        store.mark_outbound(response_key, "COMPLETED", profile, response_message_id)
        logger.info(
            "discord-final-delivery root=%s channel_id=%s message_id=%s status=sent",
            root_task_id,
            channel_id,
            message_id,
        )
        return "sent"


def humanize_user_facing_text(content: str) -> str:
    """Apply the same safe display translation used by outbound Discord."""

    return DiscordFinalDelivery._humanize_content(content)


__all__ = [
    "DiscordCorrelation",
    "DiscordFinalDelivery",
    "correlation_from_task",
    "correlation_from_tasks",
    "humanize_user_facing_text",
]
