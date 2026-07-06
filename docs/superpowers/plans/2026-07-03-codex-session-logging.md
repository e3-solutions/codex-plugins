# Codex Session Logging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a separate Codex plugin that captures full user prompts and assistant responses, catalogs them in Postgres, and stores the raw content in Supabase Storage.

**Architecture:** The plugin bundles `UserPromptSubmit` and `Stop` hooks. Hooks append local JSONL events immediately, write full message bodies to bucket-style local spool files, and queue upload-ready records whose canonical remote target is a private Supabase Storage bucket plus small Postgres index rows.

**Tech Stack:** Python hook scripts, Codex plugin manifests/hooks, Supabase Postgres, Supabase Storage, pytest.

---

## File Structure

- Create `plugins/codex-session-logging/.codex-plugin/plugin.json`: plugin manifest.
- Create `plugins/codex-session-logging/hooks/hooks.json`: `UserPromptSubmit` and `Stop` hook registration.
- Create `plugins/codex-session-logging/scripts/session_logging.py`: capture, local spool, config, and queue logic.
- Create `plugins/codex-session-logging/scripts/user_prompt_submit.py`: thin hook entrypoint for user prompts.
- Create `plugins/codex-session-logging/scripts/stop.py`: thin hook entrypoint for assistant messages.
- Create `plugins/codex-session-logging/supabase/migrations/001_codex_session_logging.sql`: schema, indexes, RLS policies, and private bucket metadata.
- Create `plugins/codex-session-logging/README.md`: setup and privacy notes.
- Modify `.agents/plugins/marketplace.json`: expose the new plugin in the repo marketplace.
- Create `tests/test_codex_session_logging.py`: behavior tests for prompt/assistant capture and config.

## Tasks

### Task 1: Capture Messages Locally

- [ ] **Step 1: Write failing tests** in `tests/test_codex_session_logging.py` that import `session_logging.py`, call `capture_hook_event()` with `UserPromptSubmit` and `Stop` payloads, and assert:
  - full message text is written to `messages/<seq>-<role>.json`;
  - local event rows only store excerpt, hash, byte size, storage path, session id, turn id, and role;
  - `transcript_path` is preserved as metadata, not parsed.
- [ ] **Step 2: Run** `pytest tests/test_codex_session_logging.py -q` and confirm it fails because the module does not exist.
- [ ] **Step 3: Implement** `session_logging.py`, `user_prompt_submit.py`, and `stop.py` with minimal behavior to pass.
- [ ] **Step 4: Run** `pytest tests/test_codex_session_logging.py -q` and confirm it passes.

### Task 2: Package Plugin and Marketplace

- [ ] **Step 1: Add failing tests** that assert the plugin manifest, hooks config, and marketplace entry exist and point at the new hook scripts.
- [ ] **Step 2: Run** `pytest tests/test_codex_session_logging.py -q` and confirm failures for missing files.
- [ ] **Step 3: Add plugin manifest, hook config, README, and marketplace entry.
- [ ] **Step 4: Run** `pytest tests/test_codex_session_logging.py -q`.

### Task 3: Supabase Schema

- [ ] **Step 1: Add failing tests** that read the migration SQL and assert it creates `codex_sessions`, `codex_session_messages`, `codex_session_events`, enables RLS, creates ownership policies, and configures a private `codex-sessions` bucket.
- [ ] **Step 2: Run** `pytest tests/test_codex_session_logging.py -q`.
- [ ] **Step 3: Add migration SQL.
- [ ] **Step 4: Apply the migration to Supabase project `pmdfllwuctzkdjiehezq` after the project reports active.
- [ ] **Step 5: Verify with SQL queries against the project.

### Task 4: Publish

- [ ] **Step 1: Run full test suite with `pytest -q`.
- [ ] **Step 2: Validate the new plugin manifest.
- [ ] **Step 3: Inspect `git diff` and stage only intended files.
- [ ] **Step 4: Commit, push branch `arya/cor-2442-add-codex-session-logging-to-supabase`, and update draft PR #6.
