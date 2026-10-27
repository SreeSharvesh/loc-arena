# Specification index

This file is the entry point to the repository's component specifications. Markdown links serve as pointers;
the detailed contracts live with their components, without duplicated text or filesystem symlinks.

| Component | Specification | Implementation guidance |
|---|---|---|
| Central IAM and service authorization | [IAM service spec](docs/iam-service/spec.md) | [IAM guide](docs/iam-service/agent.md); [repository instructions](AGENTS.md) |
| Per-agent logging and Inspect export | [Agent-log spec](docs/agent-log/spec.md) | [Agent-log guide](docs/agent-log/agent.md) |
| The job service and its fleet | [Job service](docs/jobsvc-service.md) | [IAM guide](docs/iam-service/agent.md) |
| The eval service | [Eval service](docs/evalkit-service.md) | [IAM guide](docs/iam-service/agent.md) |
| The tickets service | [Tickets service](docs/tickets-service.md) | [IAM guide](docs/iam-service/agent.md) |
| The notes service | [Notes service](docs/notes-service.md) | [IAM guide](docs/iam-service/agent.md) |

## Rules for component specifications

- Every agent-facing service must get its identity, rights and access requests from central IAM; a new or
  changed one adds no local mechanism for them. Its operation declaration and component-specific behavior
  belong in its own spec; it does not duplicate central authorization rules.
- Specs distinguish implemented behavior from proposed work and give acceptance checks for completion.
- Keep one authoritative contract per component. Add a link here when introducing a new component spec.
- Component rules must preserve the repository's isolation, sealed/mirror boundary and independent grading
  requirements in [AGENTS.md](AGENTS.md).

Central IAM and its shared integration exist, and every stack run puts the services under them: `iam.enabled`
is on unless a run config turns it off, which only the in-process deterministic fixture does. Section 11 of the
IAM spec says which services are under IAM.
