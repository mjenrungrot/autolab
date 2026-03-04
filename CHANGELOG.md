# Changelog

## [1.2.2] - 2026-03-03

### Summary

- Enforced strict pre-commit changelog validation for the exact release hop `v<previous>..v<current>`.
- Added changelog tooling to scaffold sections, validate release ranges, and render release-note bodies.
- Wired release CI to validate `CHANGELOG.md` and publish notes from the version-scoped changelog section.

<!-- autolab:range v1.2.1..v1.2.2 -->

## [1.2.0] - 2026-03-03

### Summary

- Added an onboarding-focused TUI cockpit flow with rendered prompt preview and guided actions.
- Expanded stage and verifier documentation to clarify workflow ownership, artifacts, and policy behavior.
- Hardened release automation by keeping hook-based version/tag sync behavior aligned with CI workflow checks.

<!-- autolab:range v1.1.70..v1.2.0 -->
