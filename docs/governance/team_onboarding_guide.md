# Team Onboarding Guide — Claude + VS Code + Git

## 1. Tools to Install
- VS Code
- Git
- GitHub account
- Claude (browser or VS Code integration)
- Python 3.10+ (optional, for running modules)

## 2. Clone the Repo
Open VS Code → Terminal:
```
git clone <repo-url>
cd <your-repo-name>
```
`<your-repo-name>` is a placeholder — use whatever this repo was actually
named when it was created from the template, not a literal value.

## 3. How We Work
Claude is the primary engine for:
- PRDs
- Architecture
- Data pipelines
- Code modules
- Tests
- READMEs
- Decision logs

VS Code is used for:
- Editing files
- Running code
- Git commits
- Git pushes
- Pull Requests

GitHub is used for:
- Branch protection
- CODEOWNERS enforcement
- Reviews
- CI/CD

## 4. Daily Workflow
1. Open Claude
2. Paste `prompts/master_prompt.md`
3. Paste your task (use `prompts/claude_prompt_library.md` prompts)
4. Claude generates output
5. Copy output into the correct file(s) in VS Code
6. Save files
7. Commit changes
8. Push branch
9. Open Pull Request
10. Wait for CODEOWNERS approval

## 5. Branching Model
- main → production
- dev → integration
- feature/<module>/<task> → individual work

## 6. Pull Request Rules
- No direct commits to main or dev
- PR required for all changes
- CODEOWNERS must approve
- CI/CD must pass
- No self-approval

## 7. Folder Ownership
- `prompts/` → Raghavan + Tech Leads
- `docs/architecture/` → Architects
- `docs/prd/` → Product Team
- `src/` → Engineering
- `tests/` → Engineering
- `config/` → Engineering + DevOps

## 8. Running Code (Optional)
```
pip install -r requirements.txt
python src/ingestion/ingest.py
python src/processing/transform.py
python src/storage/save.py
pytest
```

## 9. Troubleshooting
- PR blocked → missing CODEOWNERS approval
- CI failing → tests failing or lint errors
- Merge blocked → protected branch rules
- Claude output too generic → forgot to paste master_prompt.md
- Copilot not relevant → this project is Claude-only

## 10. Expectations
- Use Claude for all generation
- Use VS Code for editing + Git
- Follow branch protection rules
- Respect CODEOWNERS
- Commit only via PRs
- Maintain documentation
