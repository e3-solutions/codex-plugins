from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    ROOT
    / "plugins"
    / "codex-session-logging"
    / "supabase"
    / "migrations"
    / "20260909230000_advance_codex_session_updated_at.sql"
).read_text(encoding="utf-8")


def test_discovery_watermark_is_atomic_owned_and_service_role_only():
    normalized = " ".join(MIGRATION.lower().split())

    assert "security definer set search_path = ''" in normalized
    assert "updated_at = greatest(" in normalized
    assert "updated_at + interval '1 microsecond'" in normalized
    assert "and user_id::text = p_user_id" in normalized
    assert "from public, anon, authenticated" in normalized
    assert "to service_role" in normalized
