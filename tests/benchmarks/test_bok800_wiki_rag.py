import tempfile
import unittest
from pathlib import Path

from benchmarks.quantization.bok800_wiki_rag import load_bok800_wiki


PAGE = """---
term: {term}
source_pdf_page: {page}
---

# {term}

## 원문 기반 정의

{definition}

## 연관검색어

{related}

## 출처

- repeated source boilerplate
"""


class Bok800WikiRagTest(unittest.TestCase):
    def test_bm25_seed_and_related_page_are_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entities = root / "wiki" / "entities"
            entities.mkdir(parents=True)
            (entities / "001.md").write_text(
                PAGE.format(
                    term="가계순저축률",
                    page=2,
                    definition="Savings rate uses disposable income.",
                    related="- 가계처분가능소득",
                ),
                encoding="utf-8",
            )
            (entities / "002.md").write_text(
                PAGE.format(
                    term="가계처분가능소득",
                    page=3,
                    definition="Disposable income is available for consumption and saving.",
                    related="- 없음",
                ),
                encoding="utf-8",
            )
            index = load_bok800_wiki(root)
            self.assertTrue(index.has_exact_term("가계 순저축률 계산"))
            injected, metadata = index.inject("ORIGINAL", query="calculate savings rate")
            self.assertTrue(metadata["hit"])
            self.assertEqual(metadata["terms"], ["가계순저축률", "가계처분가능소득"])
            self.assertIn("Savings rate", injected)
            self.assertTrue(injected.endswith("ORIGINAL"))

    def test_source_boilerplate_does_not_create_a_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entities = root / "wiki" / "entities"
            entities.mkdir(parents=True)
            (entities / "001.md").write_text(
                PAGE.format(term="결제", page=9, definition="Settlement definition.", related="- 없음"),
                encoding="utf-8",
            )
            index = load_bok800_wiki(root)
            self.assertEqual(index.search("repeated source boilerplate"), [])


if __name__ == "__main__":
    unittest.main()
