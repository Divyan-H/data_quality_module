# Team Access Matrix — Claude-only

Permission levels below use GitHub's native repository permission names
(`Read`, `Triage`, `Write`, `Maintain`, `Admin`) so they map directly to
Settings → Collaborators & teams when a repo is created from this template.
GitHub does not scope push access by folder — the "Allowed Actions" column
below is enforced through required CODEOWNERS review (see `CODEOWNERS` and
`branch_protection_policy.md`), not through GitHub's permission system
itself.

| Role             | GitHub Permission | Allowed Actions                                  |
|------------------|--------------------|--------------------------------------------------|
| Owner (Raghavan) | Admin              | Full control, manage settings, approve anything    |
| Admins           | Admin              | Manage repo, branches, permissions                 |
| Tech Leads       | Maintain           | Merge PRs, approve CODEOWNERS areas                |
| Architects       | Write              | PRs to architecture + docs                         |
| Product Team     | Write              | PRDs, decision logs, docs                          |
| Engineering      | Write              | Code, tests, config                                |
| DevOps           | Write              | CI/CD, config, infra                               |
| Viewers          | Read               | No write access                                    |

## Special Rules
(Enforced via CODEOWNERS-required review, not GitHub permission scoping —
see note above.)
- Prompts folder: only Raghavan + Tech Leads
- Architecture folder: only Architects
- PRD folder: only Product Team
- Code folders: only Engineering

## Scaling this table down
This is the maximal roster, sized for a repo with a fully staffed team. A
small or solo repo doesn't need eight distinct roles filled by eight
distinct people — collapse rows onto whoever is actually doing the work
(e.g. one person can hold Tech Lead + Architect + Engineering permissions),
and drop a role entirely, along with its CODEOWNERS line, if this repo has
no one in it (e.g. no dedicated Product Team means no `docs/prd/` CODEOWNERS
restriction).
