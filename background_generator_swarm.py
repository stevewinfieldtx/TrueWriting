"""
Chimera Secured — Parallel Background Email Generator ("Swarm")
---------------------------------------------------------------
Drop-in replacement for the serial OpenRouter generator.

Why this exists: the serial generator was ~5s/email single-threaded, making 2000
emails take ~3 hours. OpenRouter handles concurrent requests cleanly; this
module runs N workers in parallel with per-request retries, rotating model
selection, and crash-safe checkpointing.

Expected speedup: ~8-12x at workers=10. 2000 emails -> ~15-20 minutes.

Usage:
    python background_generator_swarm.py --n 2000 --workers 10
    python background_generator_swarm.py --resume          # pick up from checkpoint

Env:
    OPENROUTER_API_KEY   required (CLAUDE.md convention)
    OPENROUTER_MODEL_ID  optional default model; overridden by --models rotation
"""

from __future__ import annotations

import argparse
import json
import os
import random
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import requests  # pip install requests


# ----------------------------- config ---------------------------------------

DEFAULT_MODELS = [
    # Diverse stylistic fingerprints — this is the multi-LLM diversity the
    # Kimi / Z.ai / DeepSeek reviews asked for. Adjust freely.
    "anthropic/claude-3.5-sonnet",
    "x-ai/grok-2",
    "google/gemini-2.0-flash-exp",
    "meta-llama/llama-3.3-70b-instruct",
    "mistralai/mistral-large",
    "deepseek/deepseek-chat",
    "qwen/qwen-2.5-72b-instruct",
]

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

DEFAULT_OUT = Path("background_emails.json")
DEFAULT_CKPT = Path("background_emails.ckpt.json")


# ---- Prompt bank (parameterized so fake-Steve variants diverge in style) ----
# Keep the surface distribution roughly matching your real corpus topic mix.

TOPIC_BANK = [
    "follow up on a sales lead discussed on a recent call",
    "ask a teammate to send a roster, list, or document to HR or finance",
    "request review of an attached invoice, contract, or SOW",
    "check the status of an open deal or partner introduction",
    "coordinate a meeting time with a customer or partner",
    "confirm wire transfer or vendor payment details",
    "share a quick product update with an MSP partner",
    "ping engineering about a bug or infra issue",
    "request a referral, warm intro, or reference call",
    "nudge someone on an outstanding approval",
    "thank a customer after a deployment or milestone",
    "share a link to a doc and ask for feedback by a deadline",
]

STYLE_MODES = [
    ("zero_shot", "Write like a generic US business professional. You have never seen Steve's writing."),
    ("few_shot",  "You've seen ~5 short Steve emails. Try to match his tone: short, direct, friendly, minimal punctuation, em-dash signature style."),
    ("high_fidelity", "You've studied 50+ Steve emails. Faithfully mimic his voice, cadence, sign-off habits, and typical closings. Include subtle idiosyncrasies."),
]

SYSTEM_TEMPLATE = """You are generating a single plausible business email that will be used as a NEGATIVE (impersonation) training sample.

Tier: {tier_name}
Tier guidance: {tier_guidance}

Requirements:
- Write the email body ONLY (no subject line, no "Here is the email:" preamble).
- Task: the email should {topic}.
- Length: {length_hint}.
- Sign off as Steve or ---Steve. Include a plausible-looking contact block only if the tier is high_fidelity.
- Do not include any meta-commentary. Output the email body and nothing else.
"""


# ----------------------------- data types -----------------------------------

@dataclass
class GenTask:
    idx: int
    tier: str
    tier_guidance: str
    topic: str
    length_hint: str
    model: str


@dataclass
class GenResult:
    idx: int
    tier: str
    model: str
    body: str
    latency_s: float
    error: Optional[str] = None


@dataclass
class RunState:
    completed: list = field(default_factory=list)
    failed_ids: list = field(default_factory=list)

    def save(self, path: Path):
        path.write_text(json.dumps(
            {"completed": self.completed, "failed_ids": self.failed_ids},
            ensure_ascii=False, indent=2
        ))

    @classmethod
    def load(cls, path: Path) -> "RunState":
        if not path.exists():
            return cls()
        d = json.loads(path.read_text())
        return cls(completed=d.get("completed", []), failed_ids=d.get("failed_ids", []))


# ----------------------------- generator core -------------------------------

def _length_hint() -> str:
    # Mix the distribution: lots of short emails (this is Steve's real shape)
    r = random.random()
    if r < 0.55:
        return "20-40 words, very short"
    if r < 0.85:
        return "40-90 words"
    return "90-180 words"


def build_tasks(n: int, models: list[str], rng: random.Random) -> list[GenTask]:
    tasks: list[GenTask] = []
    for i in range(n):
        tier_name, tier_guidance = rng.choice(STYLE_MODES)
        topic = rng.choice(TOPIC_BANK)
        tasks.append(GenTask(
            idx=i,
            tier=tier_name,
            tier_guidance=tier_guidance,
            topic=topic,
            length_hint=_length_hint(),
            model=rng.choice(models),
        ))
    return tasks


def call_openrouter(task: GenTask, api_key: str, timeout: int = 60,
                    max_retries: int = 3) -> GenResult:
    system = SYSTEM_TEMPLATE.format(
        tier_name=task.tier,
        tier_guidance=task.tier_guidance,
        topic=task.topic,
        length_hint=task.length_hint,
    )
    payload = {
        "model": task.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": "Write the email now."},
        ],
        "temperature": 0.9,
        "max_tokens": 400,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://chimera-secured.local",
        "X-Title": "Chimera Wave2 Background Gen",
    }

    t0 = time.time()
    last_err = None
    for attempt in range(max_retries):
        try:
            r = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=timeout)
            if r.status_code == 429:
                # rate limited — exponential backoff
                time.sleep(2 ** attempt + random.random())
                continue
            r.raise_for_status()
            data = r.json()
            body = data["choices"][0]["message"]["content"].strip()
            return GenResult(
                idx=task.idx, tier=task.tier, model=task.model,
                body=body, latency_s=time.time() - t0,
            )
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            time.sleep(1.5 ** attempt + random.random())

    return GenResult(
        idx=task.idx, tier=task.tier, model=task.model, body="",
        latency_s=time.time() - t0, error=last_err or "unknown",
    )


# ----------------------------- orchestration --------------------------------

class Swarm:
    def __init__(self, workers: int, api_key: str, out_path: Path, ckpt_path: Path,
                 ckpt_every: int = 25):
        self.workers = workers
        self.api_key = api_key
        self.out_path = out_path
        self.ckpt_path = ckpt_path
        self.ckpt_every = ckpt_every
        self.state = RunState.load(ckpt_path)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._done_since_ckpt = 0
        self._t_start = 0.0

    def _install_sigint(self):
        def handler(signum, frame):
            if self._stop.is_set():
                print("\n[swarm] second Ctrl+C — hard exit.")
                sys.exit(1)
            print("\n[swarm] Ctrl+C received — draining inflight workers, saving checkpoint…")
            self._stop.set()
        signal.signal(signal.SIGINT, handler)

    def _flush_checkpoint(self):
        self.state.save(self.ckpt_path)
        # also persist the final-shape file so downstream training can read partial
        self.out_path.write_text(json.dumps(self.state.completed, ensure_ascii=False, indent=2))

    def run(self, tasks: list[GenTask]):
        done_ids = {c["idx"] for c in self.state.completed}
        pending = [t for t in tasks if t.idx not in done_ids]
        total = len(tasks)
        remaining = len(pending)
        if not pending:
            print("[swarm] nothing to do — all tasks already complete in checkpoint.")
            return

        print(f"[swarm] workers={self.workers}  total={total}  remaining={remaining}  "
              f"resuming={total - remaining}")
        self._install_sigint()
        self._t_start = time.time()

        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            futures = {ex.submit(call_openrouter, t, self.api_key): t for t in pending}
            completed_run = 0
            for fut in as_completed(futures):
                if self._stop.is_set():
                    break
                task = futures[fut]
                try:
                    res = fut.result()
                except Exception as e:  # noqa: BLE001
                    res = GenResult(idx=task.idx, tier=task.tier, model=task.model,
                                    body="", latency_s=0.0, error=str(e))

                with self._lock:
                    if res.error or not res.body:
                        self.state.failed_ids.append(task.idx)
                    else:
                        self.state.completed.append({
                            "idx": res.idx,
                            "tier": res.tier,
                            "model": res.model,
                            "body": res.body,
                            "latency_s": round(res.latency_s, 2),
                        })
                    self._done_since_ckpt += 1
                    completed_run += 1

                    if self._done_since_ckpt >= self.ckpt_every:
                        self._flush_checkpoint()
                        self._done_since_ckpt = 0

                    # live ETA
                    elapsed = time.time() - self._t_start
                    rate = completed_run / elapsed if elapsed > 0 else 0
                    eta_s = (remaining - completed_run) / rate if rate > 0 else 0
                    ok = len(self.state.completed)
                    fail = len(self.state.failed_ids)
                    print(f"[swarm] {ok}/{total} ok  fail={fail}  "
                          f"rate={rate:.1f}/s  eta={eta_s/60:.1f}m  "
                          f"last={res.tier}/{res.model.split('/')[-1][:18]}",
                          end="\r", flush=True)

        print()
        self._flush_checkpoint()
        ok = len(self.state.completed)
        fail = len(self.state.failed_ids)
        elapsed = time.time() - self._t_start
        print(f"[swarm] done. ok={ok} fail={fail} elapsed={elapsed/60:.1f}m  "
              f"out={self.out_path}")
        if fail:
            print(f"[swarm] {fail} failures will retry on next run "
                  f"(run with --retry-failed to re-queue them).")


# ----------------------------- cli ------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2000, help="total emails to generate")
    ap.add_argument("--workers", type=int, default=10, help="concurrent API calls")
    ap.add_argument("--models", nargs="*", default=DEFAULT_MODELS)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--ckpt", type=Path, default=DEFAULT_CKPT)
    ap.add_argument("--ckpt-every", type=int, default=25)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--resume", action="store_true", help="resume from checkpoint only")
    ap.add_argument("--retry-failed", action="store_true",
                    help="re-queue previously failed ids in addition to unfinished ones")
    args = ap.parse_args()

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("ERROR: OPENROUTER_API_KEY not set.", file=sys.stderr)
        sys.exit(2)

    rng = random.Random(args.seed)
    tasks = build_tasks(args.n, args.models, rng)

    swarm = Swarm(
        workers=args.workers,
        api_key=api_key,
        out_path=args.out,
        ckpt_path=args.ckpt,
        ckpt_every=args.ckpt_every,
    )

    if args.retry_failed:
        # clear failed list so they get re-scheduled
        swarm.state.failed_ids = []
        swarm.state.save(args.ckpt)

    swarm.run(tasks)


if __name__ == "__main__":
    main()
