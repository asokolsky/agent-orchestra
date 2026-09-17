# OpenAI Agents API runtime decision

The OpenAI Agents API is an execution harness, not a replacement for this
orchestrator. If adopted, it would belong behind the existing runtime and
adapter boundary, alongside the Codex CLI and Claude Code. It must not own the
workflow state machine, reviewer-set policy, authorization boundaries, or
canonical evidence.

The current decision is not to register it. The API now answers several early
feasibility questions: sessions are durable, a turn can be cancelled, session
turns and items can be retrieved, and a self-hosted environment names a
`workspace_directory`. In self-hosted mode, however, OpenAI still runs the
managed harness while a local `codex exec-server` process—an OpenAI Agents API
component, not the Codex CLI runtime registered by Agent Orchestra—connects the
selected environment. That is a different execution and recovery contract from
the bounded local child process used by every current adapter. See the official

- [Agents API overview](https://developers.openai.com/api/docs/guides/agents-api/overview),
- [self-hosted sandbox guide](https://developers.openai.com/api/docs/guides/agents-api/environments/self-hosted),
- [Agents API reference](https://developers.openai.com/api/reference/typescript/resources/beta/subresources/agents).

The remaining questions resolve as follows:

- Agent Orchestra would continue to own the wall-clock deadline. Sending a
  cancellation event is available, but an adapter would also need to prove that
  cancellation reached a terminal turn state and translate that state into the
  existing durable `timed_out` conclusion.
- A self-hosted environment can expose a chosen workspace directory, but the
  documented contract is not Git-specific. Existing-checkout behavior,
  exact-diff isolation, and cleanup would need an integration test before the
  environment could be trusted with a worktree.
- Sessions are retained by OpenAI, including for self-hosted environments, and
  the API does not support Zero Data Retention. Deletion removes a session from
  the public API while physical cleanup may continue asynchronously. This is an
  additional data-lifecycle boundary, not local evidence retention.
- Durable sessions can resume and their turns and items can be read back, but
  remote session state is not offline-verifiable evidence. An adapter would
  first need a completeness contract that materializes every decision-relevant
  remote item locally, hashes it, and proves that no omitted remote state could
  change the reconstructed result.

The managed sandbox, tools, and session continuity add little for the stateless,
network-disabled reviewer role. They may benefit a future developer runtime,
but only when a concrete use case justifies the new network dependency and the
capture contract above. Until then, the Codex CLI keeps execution local and fits
the existing process, timeout, recovery, and audit model without changing the
CLI schema, evidence layout, stable error codes, or audit contract.
