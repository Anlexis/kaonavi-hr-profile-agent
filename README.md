# Kaonavi HR Profile Management Agent

AI agent for managing employee profiles in Kaonavi, built with Agentic Star.

> **Category**: Cat 2 (multi-step domain workflow)
> **Industry**: Common
> **Template ID**: CMN-C2-273

## Overview

An HR-profile assistant for the Kaonavi employee-data management service. It takes a natural-language request ("look up the profile for employee code 1001", "register a new employee named “Sato Hanako”"), classifies it as a profile lookup, creation, or update, extracts the employee code, display name, and profile fields from the request text, assembles the matching Kaonavi REST API v2 `member_data` request, performs the call, and returns a human-readable confirmation naming the affected record. Intent classification and field extraction are LLM-enhanced (Azure OpenAI), always falling back to the original deterministic keyword/regex heuristic on any failure, so the pipeline still runs and tests end-to-end without a live LLM. Employee code resolution is excluded from the LLM path unconditionally — it is taken only from an explicit code in the request or the caller-supplied hint, never invented. The bundled Kaonavi API client defaults to a documented network-free stub transport, so the pipeline runs and tests end-to-end without a live Kaonavi tenant; injecting live HTTP transports and an API token switches it to real calls without business-logic changes.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. The agent imports its base classes from the framework package at start-up, so without that
package installed and configured, import and graph compile fail outright rather than leaving the
agent running in a partially working state. This is intentional — a half-running agent is worse
than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and operational documentation
```

See `docs/` for the design and the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.

