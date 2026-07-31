#!/usr/bin/env python3
"""
Tavily API를 통해 금융 관련 뉴스 및 공시자료를 가져오는 스크립트
사용법: python3 departments/01-research/collectors/news.py [검색어]
(구 경로 fetch_news.py 와 임시 호환 Wrapper는 2026-07-30에 삭제됐다)
"""
import json
import sys
import os
import urllib.request

def fetch_news(query, max_results=5):
    """Tavily API를 통해 뉴스 검색"""
    api_key = os.environ.get('TAVILY_API_KEY', '')
    if not api_key:
        # .env 파일은 저장소 루트에 있다 (repo root, 이 스크립트 자신의 디렉터리가 아님)
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
        env_path = os.path.join(repo_root, '.env')
        if os.path.exists(env_path):
            # .env 는 UTF-8이다. encoding을 생략하면 한국어 Windows에서
            # locale 기본값(cp949)으로 열려 주석의 비ASCII 문자에서 깨진다.
            with open(env_path, encoding='utf-8') as f:
                for line in f:
                    if line.startswith('TAVILY_API_KEY='):
                        api_key = line.split('=', 1)[1].strip()
                        break
    
    if not api_key:
        return {"error": "TAVILY_API_KEY not found in environment or .env file"}
    
    url = 'https://api.tavily.com/search'
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}'
    }
    
    search_query = f"{query} stock news analysis financial"
    
    data = json.dumps({
        'query': search_query,
        'search_depth': 'basic',
        'max_results': max_results,
        'include_raw_content': False
    }).encode()
    
    try:
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            return result
    except Exception as e:
        return {"error": str(e)}

if __name__ == '__main__':
    # 국내 뉴스 제목이 그대로 출력되므로 stdout을 UTF-8로 고정한다.
    # cp949 콘솔에서는 ensure_ascii=False 출력이 UnicodeEncodeError로 죽는다.
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    query = sys.argv[1] if len(sys.argv) > 1 else "AAPL Apple stock"
    results = fetch_news(query)
    
    if 'error' in results:
        print(f"Error: {results['error']}")
        sys.exit(1)
    
    print(json.dumps(results, indent=2, ensure_ascii=False))
