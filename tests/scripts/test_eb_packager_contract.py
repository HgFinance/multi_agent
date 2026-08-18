from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PACKAGER = ROOT / "scripts" / "package_eb_bundle.ps1"


def test_eb_bundle_uses_git_tracked_allowlist() -> None:
    script = PACKAGER.read_text(encoding="utf-8")

    assert "ls-files --cached" in script
    assert "Get-ChildItem -LiteralPath $repo -Recurse" not in script
    assert '$names -contains ".env"' in script
    assert '$_ -like "quant-data/*"' in script
    assert "Git 추적 파일이 작업 트리에 없습니다" in script
