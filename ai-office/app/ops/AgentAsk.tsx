"use client";

// 회계·포트폴리오본부 Hermes Agent 질의 상자.
//
// 소유: 도현
// 근거: docs/02-engineering/AI_OFFICE_FRONTEND_PLAN.md 6(명령 경계)
//       docs/HEDGE_FUND_MASTER_PLAN.md 5.6
//
// 부서를 body로 보내지 않는다. 경로 자체가 회계본부 하나로 고정돼 있어서
// 이 상자로는 다른 본부 Agent를 부를 방법이 없다.
//
// 여기서 나오는 문장은 **수치가 아니다.** 위 Snapshot 카드의 NAV·Position이
// 공식 값이고, 이 답변은 참고 텍스트다(팀 가이드 원칙 5). 그래서 답변 옆에
// 출처 배지를 항상 같이 띄운다.

import { useState } from "react";

import { BFF } from "./readModel";

export default function AgentAsk() {
  const [query, setQuery] = useState("");
  const [answer, setAnswer] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function ask() {
    setBusy(true);
    setError("");
    setAnswer("");
    try {
      const res = await fetch(`${BFF}/accounting/agent/ask`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query }),
      });
      const body = await res.json();
      // 503은 고장이 아니라 기본 상태다. Auth·Tool Allowlist 전까지 닫혀 있다.
      if (!res.ok) setError(body.detail ?? `HTTP ${res.status}`);
      else setAnswer(body.answer);
    } catch (e) {
      setError(`BFF 연결 실패 — uvicorn이 떠 있는지 확인하세요 (${String(e)})`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="win-body" aria-label="회계본부 Agent 질의">
      <div className="section-heading">
        <div>
          <p className="eyebrow">회계·포트폴리오본부</p>
          <h2>Agent에게 묻기</h2>
        </div>
        <span className="status-pill waiting">참고용</span>
      </div>

      <p className="dash-note">
        Hermes 부서 Agent의 <b>텍스트 응답</b>입니다. 위 카드의 NAV·Position이 공식 수치이고, 여기
        문장에서 숫자를 옮겨 적지 않습니다.
      </p>

      <textarea
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        rows={2}
        maxLength={2000}
        placeholder="예: 오늘 대사에서 확인할 항목은?"
        style={{ width: "100%" }}
      />
      <button className="btn btn-small" onClick={ask} disabled={busy || !query.trim()}>
        {busy ? "묻는 중… (최대 60초)" : "묻기"}
      </button>

      {error && <p className="dash-note">⚠️ {error}</p>}
      {answer && (
        <p className="dash-note">
          <span className="status-pill">비공식</span> {answer}
        </p>
      )}
    </section>
  );
}
