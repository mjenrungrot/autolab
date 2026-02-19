# Skill Distribution Notes

Autolab ships canonical skill sources in `src/autolab/skills/` by provider.

## Canonical Sources

- Codex provider skills: `src/autolab/skills/codex/`
- Claude provider skills: `src/autolab/skills/claude/`

Canonical workflow operator skills:

- `src/autolab/skills/codex/autolab/SKILL.md`
- `src/autolab/skills/claude/autolab/SKILL.md`

## Installed Skill Paths

`autolab install-skill <provider>` installs packaged skill content into provider-specific project directories:

- `codex` -> `<project-root>/.codex/skills/<skill>/SKILL.md`
- `claude` -> `<project-root>/.claude/skills/<skill>/SKILL.md`

Install always writes full content copies from packaged assets.

## Contributor Rules

- Edit canonical sources under `src/autolab/skills/<provider>/...`.
- Do not edit generated installed copies under project-local `.codex/` or `.claude/` directories.
- Keep provider variants aligned on workflow semantics; only provider/runtime handling should differ.

## Bundled Scope

- Codex bundle includes workflow and orchestration skills (`autolab`, `llm-council`, `swarm-planner`, `parallel-task`).
- Claude bundle currently includes the `autolab` workflow operator skill.

To add more provider-specific skills, create `src/autolab/skills/<provider>/<skill>/SKILL.md` and ensure installer/test coverage is updated.
