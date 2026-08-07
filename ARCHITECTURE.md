# ReproGate Architecture

## Overview

ReproGate is an evidence-first verification platform for AI coding agents.

Before an AI agent modifies a repository, ReproGate attempts to reproduce the reported issue by analyzing the repository, generating a candidate reproduction test, executing it inside an isolated Docker sandbox, and producing an evidence-backed classification.

AI assists with repository understanding, relevant file discovery, test generation, and explanation.

Deterministic execution inside Docker is the source of truth.

The system is designed as a modular monolith with a React frontend, FastAPI backend, PostgreSQL database, and Docker-based execution engine.

## High-Level Architecture

```text
                    React Frontend
                           │
                           ▼
                    FastAPI Backend
                           │
      ┌────────────────────┼────────────────────┐
      │                    │                    │
      ▼                    ▼                    ▼
 GitHub Service      LLM Service        PostgreSQL
      │                    │
      └──────────────┬─────┘
                     ▼
            Verification Engine
                     │
      ┌──────────────┼──────────────┐
      ▼              ▼              ▼
Repository      Test Generator   Classification
 Analyzer
                     │
                     ▼
              Docker Sandbox
                     │
                     ▼
             Execution Evidence
```

## Backend Modules

The FastAPI backend is organized by domain responsibility rather than by file type. Each module owns one part of the verification workflow.

```text
backend/
└── app/
    ├── api/
    │   ├── routes/
    │   └── dependencies/
    │
    ├── core/
    │   ├── config.py
    │   ├── logging.py
    │   └── exceptions.py
    │
    ├── github/
    │   ├── client.py
    │   ├── issue_service.py
    │   └── repository_service.py
    │
    ├── repository_analysis/
    │   ├── detector.py
    │   ├── file_indexer.py
    │   ├── context_retriever.py
    │   └── models.py
    │
    ├── llm/
    │   ├── provider.py
    │   ├── openai_provider.py
    │   ├── prompts/
    │   └── schemas.py
    │
    ├── verification/
    │   ├── orchestrator.py
    │   ├── issue_analyzer.py
    │   ├── test_generator.py
    │   ├── evidence_builder.py
    │   └── classifier.py
    │
    ├── sandbox/
    │   ├── runner.py
    │   ├── docker_client.py
    │   ├── limits.py
    │   └── models.py
    │
    ├── persistence/
    │   ├── database.py
    │   ├── models/
    │   └── repositories/
    │
    ├── schemas/
    │
    └── main.py
```

### Module Responsibilities

#### `api`

Exposes HTTP endpoints, validates incoming requests, and converts domain results into API responses. It must not contain repository analysis, LLM, Docker, or classification logic.

#### `core`

Contains application-wide configuration, structured logging, exception types, and shared infrastructure concerns.

#### `github`

Fetches GitHub issue data, repository metadata, branches, commits, and source code. GitHub-specific API behavior must remain isolated inside this module.

#### `repository_analysis`

Detects the repository ecosystem, indexes files, discovers relevant code context, and identifies the existing testing framework.

#### `llm`

Defines a provider-independent LLM interface. OpenAI is the initial provider, but verification modules must not depend directly on the OpenAI SDK.

#### `verification`

Owns the end-to-end verification workflow. The orchestrator coordinates issue analysis, context retrieval, candidate test generation, sandbox execution, evidence construction, and classification.

#### `sandbox`

Executes untrusted repository code inside short-lived Docker containers with explicit resource, network, filesystem, and timeout restrictions.

#### `persistence`

Stores repositories, issues, verification runs, generated tests, execution results, evidence, classifications, and human decisions.

#### `schemas`

Contains shared Pydantic request, response, and domain data-transfer models.

### Dependency Rule

Higher-level workflow modules may depend on lower-level service interfaces, but infrastructure modules must not contain business decisions.

For example:

```text
API → Verification Orchestrator → GitHub / Analysis / LLM / Sandbox / Persistence
```

The API layer must never call Docker or OpenAI directly.

## Database Design

The VerificationRun is the central entity of the persistence layer.

Every execution of an issue verification creates a new VerificationRun. Generated tests, execution logs, evidence, and the final decision are attached to that run rather than directly to the GitHub issue.

This preserves historical executions, enables reproducibility, and allows comparison across repository revisions, prompts, and model versions.
### Entity Relationship Diagram

```text
Repository (1)
      │
      │
      ▼
Issue (N)
      │
      │
      ▼
VerificationRun (N)
      ├──────────────┬──────────────┬──────────────┐
      ▼              ▼              ▼              ▼
GeneratedTest   ExecutionResult   Evidence    HumanDecision
```

### Tables

#### Repository

Stores information about the analyzed GitHub repository.

Fields:

- id
- github_owner
- github_name
- default_branch
- language
- package_manager
- test_framework
- cloned_at
- created_at

---

#### Issue

Stores GitHub issue metadata.

Fields:

- id
- repository_id
- github_issue_number
- title
- description
- state
- author
- labels
- created_at

---

#### VerificationRun

Represents one complete verification attempt.

Fields:

- id
- issue_id
- status
- repository_commit
- started_at
- completed_at
- llm_provider
- llm_model
- classification

---

#### GeneratedTest

Stores the AI-generated reproduction test.

Fields:

- id
- verification_run_id
- filename
- generated_code
- prompt_version
- generation_time

---

####ExecutionResult

- id
- verification_run_id
- exit_code
- execution_time_ms
- tests_run
- tests_passed
- tests_failed
- memory_usage_mb
- stdout
- stderr
- docker_image
---

#### Evidence

Stores structured evidence produced during execution.

Fields:

- id
- verification_run_id
- relevant_files
- execution_summary
- reproduced_behavior
- expected_behavior
- observed_behavior

---

#### HumanDecision

Stores the final human approval.

Fields:

- id
- verification_run_id
- approved
- reviewer_notes
- reviewed_at

# Decision 001

## Verification Workflow

Status: Accepted

The verification process is implemented as an asynchronous job.

The frontend creates a VerificationRun and immediately receives a run identifier.

The frontend then polls the VerificationRun until completion rather than waiting for a long-running HTTP request.

Reasoning

- Better user experience
- Supports long-running Docker execution
- Easier retries
- Enables progress tracking
- Easier future queue system

Alternatives Considered

- Single blocking `/verify` endpoint

Rejected because verification may take several minutes and would produce a poor user experience.

## REST API

### Verification

POST /api/v1/verification-runs

Creates a new verification run.

Input

- GitHub Issue URL

Response

- verification_run_id
- status

---

GET /api/v1/verification-runs/{id}

Returns the current verification status.

Possible states

- QUEUED
- CLONING
- ANALYZING
- GENERATING_TEST
- EXECUTING
- BUILDING_EVIDENCE
- COMPLETED
- FAILED

---

GET /api/v1/verification-runs/{id}/report

Returns the complete verification report including repository information, issue details, generated test, execution metrics, evidence, logs, and final classification.

---

GET /api/v1/verification-runs/{id}/logs

Returns Docker execution logs.

---

### Generated Test

GET /api/v1/verification-runs/{id}/generated-test

Returns the generated reproduction test.

---

### Human Decision

POST /api/v1/verification-runs/{id}/decision

Stores the final approval or rejection.

