#!/usr/bin/env python3
"""07-agent-workforce Modelfile 스모크 테스트.

로컬 Ollama가 서빙하는 agent-hr 모델(Modelfile 참고)이 SYSTEM 프롬프트를
따르는지 육안으로 확인한다. LLM 응답을 검증하는 assert는 없다 — 비결정적
출력이라 그건 이 스크립트의 역할이 아니다.

사전 준비:
ollama pull qwen3:1.7b
  ollama create agent-hr -f departments/07-agent-workforce/Modelfile

실행: python departments/07-agent-workforce/scripts/test_hr_ollama_agent.py
      OLLAMA_BASE_URL, OLLAMA_MODEL 환경변수로 대상 서버·모델명을 바꿀 수 있다.

파일명이 test_hr_ollama_agent.py인 이유(CI-01, 2026-08-03) - 원래 이름
test_ollama_agent.py가 00-ceo-office/05-accounting-portfolio와 겹쳐
pytest가 여러 부서 파일을 같은 모듈 이름으로 import하려다 collection에서
충돌했다. assert 없는 수동 스모크 스크립트라 pytest 대상이 될 필요도 없지만,
`test_*.py` 패턴에 걸리는 위치에 있어 이름을 부서별로 고유하게 바꿨다.
"""
from __future__ import annotations

import os

from openai import OpenAI

BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://172.31.99.238:11434/v1")
MODEL = os.environ.get("OLLAMA_MODEL", "agent-hr")

PROMPTS = [
    "너희 부서가 하지 않는 일 세 가지만 말해줘.",
    "리서치본부가 신규 Agent 채용을 요청했어. 어떤 순서로 검토해야 해?",
    "네가 만든 채용 후보를 네가 최종 승인해도 돼?",
]


def main() -> None:
    client = OpenAI(base_url=BASE_URL, api_key="ollama")
    print(f"model={MODEL} base_url={BASE_URL}\n")

    for prompt in PROMPTS:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
        )
        print(f"Q: {prompt}")
        print(f"A: {response.choices[0].message.content}\n")


if __name__ == "__main__":
    main()
