# Candidate-System Definition v0.4

## Candidate identity

A candidate is the entire executable AI system under evaluation, not merely the base model.

Canonical identity:

```text
CandidateID =
hash(
  provider +
  model_id +
  model_version +
  system_prompt_hash +
  task_prompt_hashes +
  preprocessing_version +
  retrieval/index/version +
  embedding_model/version +
  reranker/version +
  tool_config +
  routing_policy +
  decoding_config +
  postprocessing +
  validator_config +
  fallback_config +
  code_commit
)
```

## Examples

These are different candidates:

- same model + different system prompt
- same model + RAG vs no RAG
- same model + different embedding model
- same model + different validator
- same model + temperature 0 vs 0.7
- same model + different retrieval top-k

These are usually the same candidate version:
- repeated execution with identical immutable configuration.

## Candidate comparison

Every scorecard must show the complete candidate manifest.

Never write:
> "Model X scored 94%"

without also identifying the system configuration that produced 94%.

## No hidden upgrades

If a provider silently changes the underlying model/version, the run must be marked:
> PROVIDER VERSION CHANGED / RUN NOT COMPARABLE

unless the new version is explicitly registered as a new candidate.
