# Using Claude in This Project

1. Always start a Claude session by pasting `master_prompt.md`, and state
   which model you're using per its MODEL section — don't leave it to the
   client's default.
2. Use `claude_prompt_library.md` for specific tasks (PRD, architecture,
   pipelines, code, README, review, decisions). If its section list
   differs from `master_prompt.md` or a `docs/` template, follow
   `master_prompt.md`'s PRECEDENCE ORDER, not whichever you saw first.
3. Claude generates PRDs, architecture, pipelines, code, tests, and READMEs.
4. Keep Claude outputs in `/docs` and `/src` under the appropriate module folder.
5. Never bypass Pull Request + review + CI/CD — commit all generated files
   through the standard Git workflow (see `docs/governance/workflow.md`).
6. Follow the module-level workflow in `docs/governance/workflow.md` for any
   new or changed module.
