"""Evidence Bundle 조립기 패키지 - 결정론 수집·계산 계층 (담당: 재일).

scripts.py 파이프라인의 assemble_evidence 노드가 이 패키지를 쓴다.
자세한 규칙·자체 점검은 bundle.py 상단 docstring 참고.
"""
from .bundle import assemble_bundle, compute_price_context, fetch_price_context

__all__ = ["assemble_bundle", "compute_price_context", "fetch_price_context"]
