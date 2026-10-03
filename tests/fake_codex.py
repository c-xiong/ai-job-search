#!/usr/bin/env python3
"""Anonymous Codex JSONL fixture. Returns text; never writes model artifacts."""
import json
import os
import re
import sys
import time
from pathlib import Path


def emit(event):
    print(json.dumps(event), flush=True)


def main():
    args = sys.argv[1:]
    if "--help" in args:
        print("--json --output-schema --ignore-user-config --ignore-rules --ephemeral --sandbox")
        return 0
    if "--version" in args:
        print("codex-cli test")
        return 0
    if args == ["login", "status"]:
        print("Logged in using ChatGPT")
        return 0
    prompt = sys.stdin.read()
    stage = os.environ.get("JOBFLOW_STAGE", "draft")
    if os.environ.get("FAKE_CALLS"):
        with open(os.environ["FAKE_CALLS"], "a") as handle:
            handle.write(json.dumps({"stage": stage, "provider": "codex", "argv": args}) + "\n")
    mode = os.environ.get("FAKE_CODEX", "ok")
    emit({"type": "thread.started", "thread_id": "fake-codex-thread"})
    if mode in ("quota", "rate_limit", "auth"):
        message = {"quota": "weekly usage limit reached", "rate_limit": "rate limit, retry later",
                   "auth": "authentication failed"}[mode]
        emit({"type": "turn.failed", "error": {"message": message}})
        return 1
    if mode == "hang":
        time.sleep(600)
    schema = json.loads(Path(args[args.index("--output-schema") + 1]).read_text())
    data = {"response_id": schema["properties"]["response_id"]["enum"][0]}
    brief = {"schema": "jobflow.brief/1", "company": "Acme", "role": "ML Engineer",
             "location": "Zurich", "language": "en", "deadline": None, "sector": "AI",
             "role_type": "Full-time", "contact_person": None, "channel": "portal",
             "hard_conflicts": [], "keywords": ["LLM", "RAG"],
             "requirements": [{"requirement": "LLM systems", "priority": "required",
                               "evidence": "research pipeline", "status": "documented"}],
             "letter_plan": {"role_task": "Build retrieval", "role_task_source": "Posting",
                             "primary_evidence": "Research", "secondary_evidence": "Software",
                             "connection": "Useful products", "unknowns": []}}
    if mode == "conflict":
        brief["hard_conflicts"] = ["A required prerequisite conflicts with the profile"]
    for field, path in re.findall(r"^(artifact_\d+) = (.+)$", prompt, re.M):
        if path.endswith("brief.json"):
            value = json.dumps(brief)
        elif path.endswith("review.json"):
            value = json.dumps({"schema": "jobflow.review/1", "verdict": "pass", "findings": []})
        elif path.endswith("inspect.json"):
            value = json.dumps({"schema": "jobflow.inspect/1", "verdict": "clean", "issues": []})
        elif path.endswith(".tex"):
            value = ("\\documentclass{article}\n\\begin{document}\n"
                     "JOBFLOW-ANONYMOUS-FIXTURE\nJane Placeholder builds LLM and RAG products.\n"
                     "\\end{document}\n")
            if mode == "truncated":
                value = "\\documentclass{article}\n\\begin{document}\n"
            if mode == "conflict":
                value = None
        else:
            value = "# Acme\nAn anonymous software engineering posting with Python and RAG.\n" * 8
        data[field] = value
    if mode == "quota_text":
        data = {k: v.replace("Acme", "Quota Acme") if isinstance(v, str) else v for k, v in data.items()}
    if mode == "wrong_nonce":
        data["response_id"] = "old-pass"
    if mode == "extra":
        data["destination"] = "cv/my_cv.tex"
    if mode == "missing":
        data.pop("artifact_0", None)
    if mode == "null":
        data["artifact_0"] = None
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(data)}})
    if mode != "no_result":
        emit({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 200,
                                                   "cached_input_tokens": 50}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
