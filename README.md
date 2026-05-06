\\\\# 🐙🍴 Octo-Spork

**The Sovereign, Local-First AI Stack for Repo Hardening & Agentic Remediation.**

Octo-Spork is a sophisticated "fopoon" (part-spoon, part-fork) architecture designed for solo developers and security-conscious engineers. It bridges the gap between high-latency, privacy-invasive cloud LLMs and the need for private, iterative, and deep repository reviews. 

By orchestrating tools like Ollama, AgenticSeek, SearXNG, and Claude Code entirely on local hardware, Octo-Spork acts as your autonomous, privacy-first security engineer and code reviewer.

---

## ⚡ Why Octo-Spork?

When building sensitive infrastructure (like fraud detection or CTI systems), shipping your codebase to a cloud LLM provider is a security risk. Octo-Spork brings the intelligence to your code, rather than sending your code to the intelligence. 

It solves the "local AI fatigue" problem through aggressive VRAM management, automated orchestrations, and long-term agentic memory, allowing you to run 30B+ parameter models on consumer hardware without system crashes.

---

## 🏗️ Core Architecture

Octo-Spork is built on an Infrastructure-as-Code philosophy, managing a multi-container stack via a Python orchestrator:

*   **Brain:** Ollama (serving Llama 3.2, Qwen 3)
*   **Orchestration & Workflow:** n8n, AgenticSeek (Langgraph-based)
*   **Research & Grounding:** SearXNG (Strict Privacy Mode)
*   **State & Memory:** Redis, PostgreSQL, ChromaDB
*   **Remediation Engine:** Local containerized Claude Code
*   **Security Integration:** Trivy, CodeQL

---

## ✨ Key Features

### 🛡️ Grounded, Evidence-First Reviews
Octo-Spork doesn't hallucinate feedback. It integrates directly with **Trivy** and **CodeQL** to scan the filesystem. Every AI-generated PR comment includes a "Grounded Receipt" with exact file paths and line numbers.

### 🤖 Local GitHub PR Bot
A local FastAPI webhook listener interacts with your repositories via a GitHub App integration. It fetches PR diffs, chunks them for local token windows, and posts professionally formatted, emoji-coded reviews back to GitHub—all from your local machine.

### 🧠 Long-Term Sovereign Memory
Octo-Spork remembers your coding style and past mistakes:
*   **ChromaDB Vector Store:** Indexes past vulnerabilities to catch recurring "architectural debt."
*   **Correction Ledger:** Learns from your manual PR comment overrides to adjust its future tone and strictness.
*   **`CLAUDE.md` Sync:** Automatically reads and updates project-specific AI instructions.

### ⚖️ VRAM Governor & Stability Engine
Running large models locally is chaotic. Octo-Spork includes a `VRAMManager` that predicts memory usage, gracefully downgrades to smaller Coder models if VRAM is pinned, and utilizes a "Circuit Breaker" to prevent infinite agentic loops from locking up your hardware.

### 🛠️ Agentic Self-Healing
More than just a reviewer, Octo-Spork can fix the bugs it finds. Triggered via a `/octo-spork fix` PR comment, the stack spins up a sandboxed Claude Code environment to implement the fix, run local tests, and push a remediation branch.

---

## 🚀 Quick Start

### Prerequisites
*   Docker & Docker Compose v2
*   Ollama installed locally (running on port 11434)
*   Bun (for the Claude Code remediation engine)
*   At least 16GB VRAM (24GB+ recommended for 30B+ models)

### Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/yourusername/octo-spork.git
   cd octo-spork
   
```

2. **Configure Environment:**
   ```bash
   cp deploy/local-ai/.env.example deploy/local-ai/.env.local
   # Edit .env.local to add your GitHub App Tokens, preferred Ollama models, etc.
   ```

3. **Verify System Health:**
   ```bash
   python src/runner/local_ai_stack.py doctor
   ```
   *This performs a 10-point check on CPU/GPU compatibility, Docker memory limits, and PATH configurations.*

4. **Launch the Stack:**
   ```bash
   python src/runner/local_ai_stack.py up
   ```

---

## 💻 Usage Commands

Octo-Spork is managed via its central Python runner.

*   `python local_ai_stack.py up`: Staged rollout of the local containers (verifying VRAM and ports).
*   `python local_ai_stack.py down --clean`: Graceful shutdown, volume pruning, and zombie-network cleanup.
*   `python local_ai_stack.py status`: Outputs a Rich-formatted table of container health and pulled Ollama models.
*   `python local_ai_stack.py verify`: Probes API health endpoints (Redis, Postgres, Ollama) and runs a dummy scan to ensure the grounding logic is working.
*   `python local_ai_stack.py doctor --fix`: Auto-resolves OOM errors, resets GPU limits, and clears unused builder caches.

---

## 🔒 Privacy & Security

Octo-Spork is designed to be completely air-gapped from cloud AI providers:
*   **Outbound Request Guard:** Monitors and blocks containers from reaching external IPs during Local-Only mode.
*   **PII Filter:** Strips repo names and sensitive data before routing queries to SearXNG.
*   **Secret Scanner:** A high-speed regex pre-check runs before the LLM sees the code, preventing secrets from even entering the local context window.

---

## 📂 Project Structure
```text
octo-spork/
├── deploy/
│   ├── local-ai/            # Docker Compose files & overrides
│   └── claude-code/         # Bun-based agentic remediation container
├── src/
│   ├── runner/              # Core orchestrator (local_ai_stack.py)
│   ├── github_bot/          # FastAPI webhook listener & PR formatter
│   ├── observability/       # OpenTelemetry tracing & TUI dashboard
│   ├── infra/               # VRAM Manager, Circuit Breakers, Port Sentinels
│   └── memory/              # ChromaDB Vector logic & Correction Ledger
├── grounding/
│   └── rules/               # Domain-specific markdown rules (e.g., CTI, Fraud)
└── logs/                    # Automated log rotation & crash reports
```

---

## 🤝 Contributing
As a tool built for sovereign development, forks and local modifications are highly encouraged. If you build a new MCP server integration or a better VRAM scheduling algorithm, please open a PR!
```
