# GitHub Updates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Track checks below.

**Goal:** Connect installed/public launcher 3.41.31 to the repository's stable releases.
**Architecture:** Public electron-updater GitHub provider with scoped URL policy and the existing verified download/restart controller.
**Tech Stack:** Electron 45, electron-updater 6.8.11, Node tests, existing Python host and NSIS.
**Spec:** `docs/superpowers/specs/2026-11-11-github-updates-design.md`

## Constraints and review focus

- Fixed public repository, stable versions only; verify tag, filename, size and SHA-512.
- Default only absent config; explicit empty/custom sources persist.
- Preserve generic HTTPS restrictions and lifecycle restart admission.
- Check GitHub's real CDN path and repository ID; reject foreign repo IDs/hosts and downgrades.
- Preserve original checkout/staging and account/profile data; publish from curated source only.

## Tasks

- [ ] Write and run failing tests for GitHub configuration, default/persisted/disabled sources, tag matching, CDN policy and rejection cases in `app/updater.regressions.test.cjs`.
- [ ] Implement provider/default/network policy exports in `app/updater.js`; integrate in `app/main.js` and test guard-before-start ordering in `app/main.regressions.test.cjs`.
- [ ] Update Settings copy and Use GitHub action in `harness/ui.js` with meaningful UI regression tests.
- [ ] Review the complete updater change independently; run all app and affected UI tests plus actual public GitHub provider discovery/download checks in isolation.
- [ ] Bump app/package lock to 3.41.31, sync only changed authored files into curated source/new sanitized staging, and build setup/portable with precompressed-file exclusions disabled.
- [ ] Verify both actual containers, frozen host/runtime/neutral seeds/privacy and generated metadata; install 3.41.31 preserving data and verify the real installed Updates screen.
- [ ] Commit/push curated source normally; publish immutable v3.41.31 with setup/portable/metadata/checksums; verify public URLs/hashes and installed GitHub check.
