# Security policy

`drydocc` hardens servers, handles their secrets and commits on their behalf, so
a flaw in a skill can weaken every node built from it. Thank you for reporting
one privately.

## Reporting a vulnerability

Use GitHub's **private vulnerability reporting**: on this repository, go to
*Security → Report a vulnerability*. Only the maintainers can see the report.

Please **do not** open a public issue, discussion or pull request for a
vulnerability.

A useful report says:
- which skill, convention or lab component is affected;
- what an attacker, or an agent following the skill, can make happen;
- how to reproduce it — a `/lab` scenario is ideal.

You will get an acknowledgement, and a fix ships in a release whose changelog
entry credits you, unless you prefer otherwise.

## In scope

- A skill or recipe that leaves a node weaker than its documented baseline:
  SSH access, firewall, rootless containers, or file permissions on secrets.
- Any path by which a secret value reaches git, the journal, a log or a
  transcript.
- A skill that writes outside the bound node's scope, or acts on a node other
  than the one it was given.
- The lab escaping its isolation — reaching the admin's real remotes, tailnet
  or vault from a lab run.

## Out of scope

- Vulnerabilities in the software a use-case runs — report those upstream.
- The agent or model itself, and its vendor's CLI.
- A fork's own configuration choices, such as a deliberately opened port.

## Supported versions

Only the latest release is supported. Fixes are not backported. Forks pick
them up with `git pull upstream main`.
