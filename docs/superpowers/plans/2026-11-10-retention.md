# Database retention implementation plan

> **For agentic workers:** Use superpowers:executing-plans inline, with test-driven-development.

**Goal:** Apply the user-approved retention policy on the deployed comprehensive bot, without backups.

**Architecture:** Add conservative timestamps through a forward migration. A resumable maintenance component shares the existing SQLite transaction boundary and runs bounded batches from the polling loop. Persist the installation baseline, last success, progress and error in metadata; show an admin-only status view.

**Tech Stack:** Python 3.12 standard library, SQLite WAL, existing Telegram polling runtime.

**Spec:** User-approved conversation: run every 14 days, expire inactive drafts/sessions after 14 days, processed messages/successful notifications after 30 days, and original material for terminal orders 30 days after completion. Never delete financial history or unresolved business. No backup.

## Global constraints
- Preserve wallets, ledger, payments, payment_checks, audit, product snapshots and order settlement summaries.
- Retain notification event keys as small tombstones, preventing repeated delivery after payload removal.
- Protect data linked to unresolved orders, payments, receipts and notifications.
- Existing rows start their retention clock at migration time; installation starts a fresh 14-day interval.
- No backups, archive copies or live test payments. No cleanup of live data during development tests.
- Work in the existing clean code checkout; only untracked chat attachments existed before this task.

## Review focus
- Upgrade must not immediately expire old rows with unknown completion times.
- Concurrent or restarted cleanup must not lose progress or touch unresolved data.
- Old UI buttons must display a clear expired-data message without resending material.
- Replaying settled operations must not recreate cleared notification payloads.
- Cleanup failures must preserve retryability and report only sanitized errors.

## Tasks
- [x] Write retention tests and observe the expected missing-feature failure.
- [x] Add migration 009 timestamps, cleanup markers and update triggers.
- [x] Implement bounded, resumable retention and admin-only status; integrate polling.
- [x] Run targeted and full suites; review changes and update operational documentation.
- [x] Deploy only changed source/migration files, restart existing service, verify schema, service, schedule baseline and unchanged financial counts. Do not create backups or force cleanup.

## Execution ledger
- Baseline attempt failed because Windows sandbox temp directories were not writable by SQLite. Re-run with TEMP/TMP under the workspace.
- Ruling: successful notification cleanup removes payloads but preserves unique keys and delivery evidence. This prevents duplicate notifications and preserves payment-card event idempotency.
- Local full suite: 135 tests, OK (4 POSIX-only skips), before two additional migration/cancellation tests. Latest targeted suite: 15 tests, OK.
- Review found resolved cancelled bulk events incorrectly preventing expiry. Added a failing regression, extended terminal event timestamps and guards to sent/cancelled, and verified green.
- Ruling: replace the temporary systemd service with the checked-in persistent service so the bot and its retention schedule resume after a server reboot. Preserve its existing environment file and virtual environment.
- Final verification: 137 tests pass on Linux; 137 run on Windows with 4 expected POSIX-only skips. systemd-analyze verify passed. Fresh-context review found no P0/P1 issues; its cancelled-event P2 was fixed with a regression test.
- Production migration 009 applied; integrity and foreign-key checks passed. Compared row content digests for wallets (1), ledger (10), payments (1), payment_checks (2), audit (5), and original order columns (4); unchanged.
- First scheduled cleanup: 2026-11-24 11:30:02 UTC (19:30:02 Asia/Shanghai). No live cleanup forced, no backup created. Persistent zhbot.service enabled, running with zero restarts.
