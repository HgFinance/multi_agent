from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260820000200_factory_proposal_lifecycle_status.sql"
)


def test_factory_role_can_only_update_proposal_status() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert (
        "grant update (status) on research.experiment_proposals to svc_quant"
        in " ".join(sql.split())
    )
    assert "has_table_privilege(" in sql
    assert "table-wide proposal update" in sql
    assert "attribute.attname <> 'status'" in sql
    assert "immutable proposal column" in sql
