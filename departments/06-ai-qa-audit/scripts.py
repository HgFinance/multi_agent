from typing import TypedDict
from langgraph.graph import StateGraph, END
from openai import OpenAI
from run_agent import AIAgent

# ==========================================
# 1. QA 부서 내부 실무진 (LangGraph + Ollama)
# ==========================================

class QAInternalState(TypedDict):
    input_report: str     # 상위/타 부서에서 넘어온 검토 대상 보고서
    source_check: str     # [직원 1] 근거/출처 검증 결과
    reproducibility: str  # [직원 2] 재현성 평가 결과
    draft_audit: str      # [직원 3] 실무진 합성 초안 보고서

# 내부 실무용 LLM 클라이언트 (Ollama)
internal_llm = OpenAI(
    base_url="http://172.31.99.238:11434/v1",
    api_key="ollama"
)

def _call_internal_llm(prompt: str) -> str:
    res = internal_llm.chat.completions.create(
        model="agent-qa",
        messages=[{"role": "user", "content": prompt}]
    )
    return res.choices[0].message.content

# --- LangGraph 노드 (실무 직원들) ---
def _worker_source_verifier(state: QAInternalState) -> dict:
    prompt = f"다음 보고서의 주장과 근거, 출처의 타당성을 검증해라:\n{state['input_report']}"
    return {"source_check": _call_internal_llm(prompt)}

def _worker_reproducibility_checker(state: QAInternalState) -> dict:
    prompt = f"다음 보고서 내용의 재현 가능성 및 논리적 헛점을 평가해라:\n{state['input_report']}"
    return {"reproducibility": _call_internal_llm(prompt)}

def _worker_report_synthesizer(state: QAInternalState) -> dict:
    prompt = f"""
    팀원들의 검증 결과를 바탕으로 최종 QA 감사 보고서 초안을 작성하라.
    - [근거 검증 결과]: {state['source_check']}
    - [재현성 평가 결과]: {state['reproducibility']}
    """
    return {"draft_audit": _call_internal_llm(prompt)}

# --- LangGraph 워크플로우 구축 ---
builder = StateGraph(QAInternalState)
builder.add_node("verify_source", _worker_source_verifier)
builder.add_node("check_reproducibility", _worker_reproducibility_checker)
builder.add_node("synthesize_report", _worker_report_synthesizer)

builder.set_entry_point("verify_source")
builder.add_edge("verify_source", "check_reproducibility")
builder.add_edge("check_reproducibility", "synthesize_report")
builder.add_edge("synthesize_report", END)

qa_internal_pipeline = builder.compile()


# ==========================================
# 2. QA 부서장 (Hermes Agent)
# ==========================================

# AIAgent 초기화 시 미지원되는 파라미터(custom_instructions 등) 제거
hermes_qa_head = AIAgent(
    model="poolside/laguna-s-2.1:free",
    quiet_mode=True
)


# ==========================================
# 3. QA 부서 메인 인터페이스 (외부 호출용)
# ==========================================

def run_qa_department(input_report: str) -> str:
    """
    QA 부서 단독 실행 함수
    Input : 검토할 보고서 (Trading, Risk 등 타 부서 또는 시스템의 인풋)
    Output: QA 부서장 Hermes가 최종 승인한 QA 감사 보고서 (CEO 또는 타 부서 전송용)
    """
    print("[QA 부서] 1. 내부 실무진(LangGraph) 검증 개시...")
    internal_res = qa_internal_pipeline.invoke({"input_report": input_report})
    draft_audit = internal_res["draft_audit"]
    
    print("[QA 부서] 2. Hermes 부서장 최종 검토 및 정제 중...")
    
    # QA 부서장 지시사항(역할 부여)을 prompt에 포함시켜 전달
    prompt = f"""
당신은 QA 부서장 Hermes입니다. 부서 실무진(LangGraph)의 검증 결과를 총괄/정제하여 최종 대외 보고서를 작성해야 합니다.

[팀원들의 감사 초안]:
{draft_audit}

위 초안을 바탕으로 CEO 또는 타 부서에 제출할 최종 결재용 QA 감사 보고서를 깔끔하게 정리해 주세요.
"""
    final_audit_report = hermes_qa_head.chat(prompt)
    
    return final_audit_report


# ==========================================
# 단독 테스트 실행
# ==========================================
if __name__ == "__main__":
    sample_input = "Risk 보고서: AAPL 100주 매수 건 변동성 1.2%로 허용 범위 내 판단됨."
    
    print("=== QA 부서 단독 테스트 실행 ===")
    qa_output = run_qa_department(sample_input)
    
    print("\n" + "="*50)
    print("★ [QA 부서장 Hermes 최종 아웃풋] ★")
    print("="*50)
    print(qa_output)