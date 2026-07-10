# Export e3 Codex and Claude sessions

Use the standalone exporter when someone needs immediate access to original local transcripts and does not need them indexed in Supabase.

```bash
python3 scripts/export_e3_sessions.py
```

The command scans `~/.codex/sessions` and `~/.claude/projects`, includes only sessions that can be verified as belonging to a GitHub `e3-solutions` repository, and writes a ZIP to the Desktop. The archive contains original JSONL transcript structure, a manifest, repository-verification details, checksums, and counts.

Common credential patterns are redacted by default. Review the archive before sharing it because free-form messages and tool output can still contain sensitive business or customer data that no automatic filter can identify.

Useful options:

```bash
# Choose the destination.
python3 scripts/export_e3_sessions.py --output ~/Desktop/priyal-e3-sessions.zip

# Preview counts and selected paths without creating an archive.
python3 scripts/export_e3_sessions.py --dry-run

# Preserve exact source bytes for a highly trusted recipient.
python3 scripts/export_e3_sessions.py --raw --output ~/Desktop/e3-sessions-raw.zip
```

The exporter never contacts Supabase or any other network service. Sessions that cannot be verified as belonging to `e3-solutions` are omitted instead of risking unrelated personal data.
