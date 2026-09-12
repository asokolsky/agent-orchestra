# Documentation

| Document | What it is | Read it when |
|---|---|---|
| [Primer](primer.md) | A path from an unreviewed change to a review you can act on. | You are starting. |
| [CLI reference](cli.md) | Every command, option, and output document. | You know what you want and need the exact flag or field. |
| [Concepts](concepts.md) | The vocabulary: jobs, tasks, attempts, roles, runtimes, adapters, capabilities. | A term in another document is unfamiliar. |
| [Workflows](workflows.md) | The state machine, its transitions, and the recovery paths. | You need to know what a state means or how a stopped job resumes. |
| [Design](design.md) | Why the tool is built this way, and the contracts it keeps. | You are changing the tool, or need the rationale behind a constraint. |

## Role contracts

One per agent role, describing what that role is given and what it must return.
These are the protocol, not a tutorial.

- [Developer](role-developer.md)
- [Reviewer](role-reviewer.md)
- [Issue reviewer](role-issue-reviewer.md)
- [Issue creator](role-issue-creator.md)

## Where facts live

Each fact has one home, so that it can be corrected in one place:

- **How to do something** is in the primer.
- **What an option does** is in the CLI reference.
- **What a word means** is in concepts.
- **Why a decision was made** is in design.

The repository [README](../README.md) is an overview and a set of pointers. It
does not restate any of the above.
