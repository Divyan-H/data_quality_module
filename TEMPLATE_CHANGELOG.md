# Template Changelog

Tracks changes to this template repository itself (not to any repo created
from it). When you fork/instantiate this template, note the version below
in your new repo's own decision log so you know which template snapshot you
started from.

## Unreleased
- Documented that the 10 `/src` modules are a menu, not a mandatory set;
  added a post-fork checklist to `README.md`.
- Reconciled CI/CD sequencing: configuring CI is now consistently a
  prerequisite step before enabling branch-protection status checks, across
  `admin_setup.md`, `repo_setup_checklist.md`, and `branch_protection_policy.md`.
- Standardized role/permission terminology on GitHub-native levels (Read,
  Triage, Write, Maintain, Admin) in `team_access_matrix.md` and
  `branch_protection_policy.md`.
- Added a `MODEL` section and a `PRECEDENCE ORDER` section to
  `prompts/master_prompt.md` to resolve conflicts between the master
  prompt, `claude_prompt_library.md` task prompts, and `docs/` templates.
- Marked placeholder values (`CODEOWNERS` handles, the onboarding guide's
  clone path) explicitly as placeholders to replace on fork.
- Reframed `docs/architecture/high_level.md` and `data_flow.md` as
  illustrative defaults/menus rather than fixed requirements.
- Noted that `docs/governance/team_access_matrix.md` and
  `branch_protection_policy.md`'s `release/*` rules describe the maximal
  case and can be scaled down or skipped for smaller repos.
- Added "when to log a decision" guidance to `docs/decisions/decision_logs.md`.

## How to version this file going forward
Add a dated entry above "Unreleased" each time a governance doc, prompt, or
folder structure changes in a way that would affect repos instantiated
after that point.
