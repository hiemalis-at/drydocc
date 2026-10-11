<!--
This repository's `main` moves only by release snapshots, so this pull request
is a proposal: if it is accepted, a maintainer ports it into the next release,
credits you in CHANGELOG.md, and closes this pull request with a link to that
release. See CONTRIBUTING.md.
-->

**What this changes for an admin or agent**

**Issue** <!-- #123, if there is one -->

**How it was tried**
<!-- `/lab` scenario, real node, or not run — and on which distro and architecture. -->

**Checklist**

- [ ] Generic prose names no distro dialect; concrete syntax is in a `references/*-recipes.md` outcome contract
- [ ] Each fact lives at one level: invariants in `AGENTS.md`, procedure in a SKILL.md body, exact syntax in `references/`
- [ ] Skill frontmatter: `name` matches the directory; `description` says what the skill does and when to use it
- [ ] A new skill is added to the README's skill catalog
- [ ] No secret values, real hostnames or IP addresses
