# Repo Setup Checklist

## 1. Create Repo
- Create GitHub repo
- Add team members with correct roles (see team_access_matrix.md)

## 2. Add Governance Files
- CODEOWNERS
- branch_protection_policy.md
- team_access_matrix.md
- workflow.md
- team_onboarding_guide.md

## 3. Configure CI/CD
- Add tests
- Add linting
- Add security scan
(Required before step 4's "required status checks" — a status-check rule
enabled with no CI configured blocks every PR indefinitely.)

## 4. Configure Branch Protection
- Protect main, dev, release/*
- Enable required reviews
- Enable required status checks (requires step 3 to be done first)
- Enable CODEOWNERS approval
- Disable direct pushes

## 5. Add Prompt Library
- /prompts/master_prompt.md
- /prompts/claude_prompt_library.md
- /prompts/usage_guidelines.md

## 6. Add Documentation Structure
- /docs/architecture/
- /docs/prd/
- /docs/decisions/
- /docs/governance/
- /docs/readmes/

## 7. Add Code Structure
- /src/<module>/ for each module
- /tests/
- /config/

## 8. Enforce Workflow
- PRs only
- Reviews required
- No bypassing CODEOWNERS
