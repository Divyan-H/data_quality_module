# Branch Protection Policy — Claude-only

## Protected Branches
- main
- dev
- release/*

## Rules for main
- No direct pushes
- Require pull request
- Require 2 reviewers
- Require CODEOWNERS approval
- Require all CI checks to pass (enable only after CI/CD is configured —
  see `admin_setup.md` → GitHub Setup, step 4)
- Prevent force pushes
- Prevent branch deletion

## Rules for dev
- No direct pushes
- Require pull request
- Require 1 reviewer
- CI checks must pass (same CI/CD prerequisite as above)

## Rules for release/* (skip this section if this repo doesn't cut releases)
- Only Admins (Admin permission) can merge
- Only Tech Leads (Maintain permission) or Admins can push
- Tagging requires Admin approval

Role names and permission levels above match `team_access_matrix.md`.

## Additional Rules
- No self-approval
- No merge without CODEOWNERS approval
- No merge if tests fail
- No merge if security scan fails — placeholder for whichever scanner this
  repo adopts (e.g. CodeQL, Dependabot, Snyk); none is configured by the
  template, so this rule has nothing to enforce against until one is wired
  into CI
