# Admin Setup Instructions — Claude + VS Code + Git

## GitHub Setup
1. Create the repository and upload this folder structure.
2. Add CI/CD via GitHub Actions. Required before step 4's "Require status
   checks to pass" rule can be enabled — a status-check rule with no CI
   configured will block every PR indefinitely.
3. Settings → Branches → Add rule.
4. Protect `main` and `dev`.
5. Enable:
   - Require pull request before merging
   - Require approvals (reviews)
   - Require review from Code Owners
   - Require status checks to pass (requires step 2 to be done first)
   - Prevent force pushes
   - Prevent branch deletion
6. Settings → Collaborators & teams → assign roles per `team_access_matrix.md`
   (use GitHub's native permission levels: Read, Triage, Write, Maintain, Admin).
7. Add `docs/governance/CODEOWNERS` at the repo root as `.github/CODEOWNERS`
   (GitHub only reads it from `/`, `/.github`, or `/docs`).

## VS Code Setup
1. Install the GitHub Pull Requests and Issues extension (optional but useful).
2. Sign in to GitHub from VS Code.
3. Clone the repo: `git clone <repo-url>`.
4. Use VS Code's Source Control panel for commits, pushes, and PRs.

## Claude Setup
1. Open Claude (browser or VS Code integration).
2. Paste `prompts/master_prompt.md` at the start of a session.
3. Use `prompts/claude_prompt_library.md` for specific tasks.
4. Copy Claude's output into the correct files in the repo.
