# Documentation

| Document | What it is | Read it when |
|---|---|---|
| [Primer](primer.md) | A path from an unreviewed change to a review you can act on. | You are starting. |
| [CLI reference](cli.md) | Every command, option, and output document. | You know what you want and need the exact flag or field. |
| [Concepts](concepts.md) | The vocabulary: jobs, tasks, attempts, roles, runtimes, adapters, capabilities. | A term in another document is unfamiliar. |
| [Workflows](workflows.md) | The state machine, its transitions, and the recovery paths. | You need to know what a state means or how a stopped job resumes. |
| [Design](design.md) | Why the tool is built this way, and the contracts it keeps. | You are changing the tool, or need the rationale behind a constraint. |
| [OpenAI Agents API runtime decision](openai-agents-api.md) | Why the Agents API is not a registered runtime. | You are evaluating that API as an execution backend. |
| [Subagent role publication decision](subagent-role-publication.md) | Why role skills are not also published as vendor subagents. | You are evaluating another role-content install target. |
| [Omnigent comparison](omnigent-comparison.md) | What a larger meta-harness validates, what it does differently, and which lessons this project adopts. | You are evaluating the project's differentiation or a lesson from Omnigent. |

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
- **Why a decision was made** is in design or a focused decision document
  indexed above.

The repository [README](../README.md) is an overview and a set of pointers. It
does not restate any of the above.
