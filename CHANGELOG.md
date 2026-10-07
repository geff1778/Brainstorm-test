# Changelog

All notable changes to BrainStorm are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Community files: `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, `CHANGELOG.md`.
- GitHub issue forms (bug report, feature request) and a pull request template.
- `.editorconfig` and `.gitattributes` for consistent cross-editor / cross-OS behaviour.

## [2.0.0] — 2026-10-07

Full, from-scratch refactor into a production-ready, layered application.

### Added
- Layered architecture: `config` → `domain` → `services` → `realtime` / `http_api`,
  with cross-cutting `security`, `validation`, `errors` and `logging_setup`.
- GigaChat integration with JSON-schema structured output, a stricter system prompt,
  meta-option rejection and a built-in offline question bank as fallback.
- Accounts (XP, levels, coins, cosmetics), UGC with a moderation queue, a 12-level
  single-player campaign, achievements, leaderboard and room history.
- Six game modes (Classic, FFA, Team draft, Lives, Co-op, Svoya Igra), adaptive
  difficulty, jokers, hints and question rephrasing.
- Admin panel and tester/cheat mode with a suite of abilities; `/api/cheat/room_stats`.
- PWA (installable, offline shell), health/readiness probes and structured logging.
- Typed settings with production fail-fast validation; no hardcoded secrets.
- Dockerfile, `docker-compose.yml`, Render blueprint, GitHub Actions CI (lint + tests
  on Python 3.10–3.12 + image build).
- Test suite grown to 190 tests; documentation: bilingual `README.md`, `ROADMAP.md`.

### Fixed
- Security: removed hardcoded secret defaults; constant-time key comparison; rate limits
  on activation/auth/AI endpoints; `X-Forwarded-For` only trusted when `TRUST_PROXY=true`.
- Realtime: restored ~22 missing/misnamed client→server event handlers (chat, cheat panel,
  admin panel, restart, presentation mode); consistent `join_room` / `new_question` payloads.
- Logic: re-entrancy guard (`resolved_q`) prevents a question from being resolved twice
  (timer vs. fast answer vs. cheat skip); timer honours the host setting.
- AI: correct 0-based/1-based answer index handling, deduplication and robust JSON parsing.
- Auth: passwords upgraded to PBKDF2-HMAC-SHA256 with legacy-hash migration.

[Unreleased]: https://github.com/geff1778/Brainstorm-test/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/geff1778/Brainstorm-test/releases/tag/v2.0.0
