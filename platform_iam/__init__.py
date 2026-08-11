"""Platform/IAM Service — HR도 6개 투자본부도 아닌 별도 서비스.

근거: docs/02-engineering/PLATFORM_IAM_SPEC.md, 마스터플랜 4.3절
("실제 Identity와 권한 생성은 Platform/IAM Service만 한다").

departments/07-agent-workforce/ 안에 두지 않는다 - "HR은 요청까지만 하고
Platform/IAM이 실제로 만든다"는 권한 분리가 코드 구조로도 드러나야 한다.
"""
