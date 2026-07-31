from openai import OpenAI


# 영주님 (CEO / 인사): agent-ceo, agent-hr

# 재일님 (Research / Quant): agent-research, agent-quant

# 도현님 (Accounting / Trading): agent-accounting, agent-trading

# 동규님 (Risk / AI QA): agent-risk, agent-qa

client = OpenAI(
    base_url="http://172.31.99.238:11434/v1",
    api_key="ollama"
)

# 예: agent-risk 에이전트 호출
response = client.chat.completions.create(
    model="agent-risk",
    messages=[{"role": "user", "content": "오늘의 리스크 평가좀 부탁해."}]
)

print(response.choices[0].message.content)