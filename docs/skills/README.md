# Skill Distribution Notes

Autolab keeps one canonical Codex skill source in:

- `src/autolab/skills/codex/autolab/SKILL.md`

Generated/installed copies are intentionally indirect:

- `docs/skills/autolab/SKILL.md`: redirect stub for documentation readers.
- `<repo>/.codex/skills/autolab/SKILL.md`: installed copy written by `autolab install-skill codex`.

Why this indirection exists:

- Packaging: the canonical file ships with the Python package from `src/`.
- Docs stability: docs can point to a stable path without duplicating skill content.
- Install reproducibility: `autolab install-skill` always uses the canonical packaged source.

Contributor rule:

- Edit only `src/autolab/skills/codex/autolab/SKILL.md`.
- Do not edit redirect stubs.

Provider scope:

- Bundled workflow skills are intentionally Codex-first today (`autolab install-skill codex`).
- `claude` / `custom` runners are supported for execution in `verifier_policy.yaml`, but first-class packaged skills for those runners are not shipped yet.
- If non-Codex skill packaging is required, add a provider-specific canonical skill under `src/autolab/skills/<provider>/...` and extend installer support in `autolab.commands`.
