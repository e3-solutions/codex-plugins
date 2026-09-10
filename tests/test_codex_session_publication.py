from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    ROOT
    / "plugins/codex-session-logging/supabase/migrations"
    / "20260910062430_publish_commit_complete_session_changes.sql"
)
EDGE_FUNCTION = (
    ROOT
    / "plugins/codex-session-logging/supabase/functions"
    / "codex-session-ingest/index.ts"
)


def normalized(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").lower().split())


def test_change_marker_is_database_monotonic_and_non_regressing():
    sql = normalized(MIGRATION)

    assert "before update on public.codex_sessions" in sql
    assert "new.updated_at = pg_catalog.greatest(old.updated_at, new.updated_at)" in sql
    assert "session.updated_at + interval '1 microsecond'" in sql
    assert "pg_catalog.clock_timestamp()" in sql
    assert "and session.user_id::text = p_user_id" in sql
    assert "if published_at is null then raise no_data_found" in sql


def test_change_marker_rpc_is_service_role_only():
    sql = normalized(MIGRATION)

    assert (
        "revoke all on function public.publish_codex_session_change(text, text) "
        "from public, anon, authenticated"
    ) in sql
    assert (
        "grant execute on function public.publish_codex_session_change(text, text) "
        "to service_role"
    ) in sql


def test_edge_function_publishes_only_after_child_catalog_writes():
    source = normalized(EDGE_FUNCTION)
    marker = "await publishsessionchangemarker(sessionid, userid)"

    assert source.count(marker) == 3
    assert (
        "await upsertmessage(record, userid, storagepath); " + marker
    ) in source
    assert (
        "await upsertevent(record, userid, storagepath, sanitizedevent); " + marker
    ) in source
    assert (
        "await upsertevent(catalogrecord, userid, storagepath, event); " + marker
    ) in source
