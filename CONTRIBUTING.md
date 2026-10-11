# Contributing to `drydocc`

Thanks for looking. `drydocc` gets better from reports out of real fleets —
a distro nobody has run it on, a service that would not adopt cleanly, a skill
that misread a box — and from proposals that fix them.

## How this repository moves

`main` here changes only by **release**: each release is one snapshot commit,
tagged and listed in [`CHANGELOG.md`](CHANGELOG.md). Forks pull those
releases from `upstream` (see [`FORKING.md`](FORKING.md) § *Staying current*).
Nobody pushes to `main` directly, maintainers included.

That shapes how contributions land:

| You have | Do | What happens |
|---|---|---|
| A bug, or a box it failed on | Open an issue ([bug report](.github/ISSUE_TEMPLATE/bug_report.md)) | Triaged; fixed in a release |
| An idea for a skill or a convention | Open an issue ([skill or convention proposal](.github/ISSUE_TEMPLATE/skill_proposal.md)) first | Discussed before anyone writes it |
| A change you have already made | Open a pull request — as a **proposal** | If accepted, a maintainer ports it into the next release, credits you in the changelog, and closes the pull request with a link to that release |
| A security problem | **Do not open an issue.** See [`SECURITY.md`](SECURITY.md) | Handled privately |

Your pull request is not merged as-is, but your change, your reasoning and your
credit carry over.

## A useful bug report

Agents make mistakes in specific ways, so the specifics matter:

- **The command and its arguments**, e.g. `/apply-use-case nextcloud`, and
  which surface it ran on (on the node, or from your laptop).
- **The node's `system_facts`** from `nodes/<node>/node.yaml`: distro,
  service manager, firewall tool and the other facts the run probed.
- **The journal entry** the run wrote under `nodes/<node>/journal/` — it
  records what was attempted and where it stopped.
- **The agent and model** (e.g. Claude Code, Mistral Vibe) and the `drydocc`
  release you are on.

Redact hostnames, IP addresses and anything else you consider private. Never
paste a secret value: the journal and the manifests are designed to hold none,
and an issue is public.

## A good proposal

The framework's own rules apply to any change, and they all ship with it:

- **[`AGENTS.md`](AGENTS.md) invariants.** Node scoping, the append-only
  journal, secrets out of git, conventions as the authority, distro-agnosticism,
  and *one fact, one level*.
- **Distro-agnosticism.** Generic prose never names a package manager,
  firewall tool, init system or MAC framework. Concrete syntax lives only in a
  workflow skill's `references/*-recipes.md`, as an outcome contract. The
  contract is
  [`skills/conventions/references/distro-agnosticism.md`](skills/conventions/references/distro-agnosticism.md).
  A recipe for a new dialect is one of the most useful contributions there is:
  verify it on a real box and add the row to that section's *Observed dialects*
  table.
- **Skills follow the [Agent Skills](https://agentskills.io) format.** That
  means a `name` that matches the directory and a `description` that says what
  the skill does and when to use it. Procedure goes in the body, exact commands
  and schemas in `references/`. A skill must be complete on its own: it states
  its rules, and never sends the reader elsewhere to find them.
- **Conventions are schemas.** A new field, file or enum value in the
  `conventions` skill is a framework change. Open an issue first, because every
  fork's data has to stay valid.
- **Try it before you propose it.** `/lab` runs your changed skills against
  throwaway local VMs, and can judge a scenario by assertions. Say in the pull
  request what you ran, and on which distro and architecture.

## Changing skills in your own fork

Your fork is yours. Editing a skill there is fine, and a later `git pull upstream main` merges
like any other upstream change. Expect a conflict only where you and a release
touched the same file. If the change would help others, propose it here.

## License

By contributing, you agree that your contribution is licensed under the
[Apache License 2.0](LICENSE), the license of this repository.
