#!/usr/bin/env python3
"""arena.py — Two AI agents bid to fix your broken command.

One runs Claude (cloud, costs money, smarter).
One runs Ollama (local, free, smaller brain).
They race. The winner's fix runs in a sandbox. If it works, they get paid.

This is a proof-of-concept for the PatchDAO bidding protocol.
"""

import subprocess
import hashlib
import json
import os
import sys
import time
import threading

# Import from fix tool
sys.path.insert(0, os.path.dirname(__file__))

# --- Config ---
CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-haiku-4-5-20251001"
OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "qwen2.5-coder:1.5b"

# --- Colors ---
C_RESET = "\033[0m"
C_RED = "\033[31m"
C_GREEN = "\033[32m"
C_YELLOW = "\033[33m"
C_BLUE = "\033[34m"
C_MAGENTA = "\033[35m"
C_CYAN = "\033[36m"
C_DIM = "\033[2m"
C_BOLD = "\033[1m"

if not sys.stderr.isatty():
    C_RESET = C_RED = C_GREEN = C_YELLOW = C_BLUE = C_MAGENTA = C_CYAN = C_DIM = C_BOLD = ""


def log(agent_name, color, icon, msg):
    ts = time.strftime("%H:%M:%S")
    print(f"  {C_DIM}{ts}{C_RESET} {color}[{agent_name:>6}]{C_RESET} {icon}  {msg}")


# --- Environment ---

def get_env_info():
    import platform, shutil
    info = {
        "os": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "distro": "",
        "shell": os.environ.get("SHELL", ""),
        "python": platform.python_version(),
    }
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    info["distro"] = line.split("=", 1)[1].strip().strip('"')
                    break
    except FileNotFoundError:
        pass
    pms = [pm for pm in ["apt", "dnf", "pacman", "brew", "pip", "npm", "cargo"]
           if shutil.which(pm)]
    info["package_managers"] = pms
    return info


def build_prompt(command, stderr_text, env_info):
    return f"""You are a DevOps agent competing in an auction. A command failed. Generate the best fix you can.

FAILED COMMAND: {command}

ERROR OUTPUT:
{stderr_text[-2000:]}

SYSTEM INFO:
- OS: {env_info.get('distro', '')} ({env_info['os']} {env_info['release']})
- Arch: {env_info['machine']}
- Shell: {env_info['shell']}
- Python: {env_info['python']}
- Package managers: {', '.join(env_info.get('package_managers', []))}

Respond with ONLY a JSON object:
{{"fix": "shell command(s) to run", "explanation": "one line why", "confidence": 0.9, "retry": true}}

"confidence" is 0.0-1.0 how sure you are this will work.
Use sudo for apt/dnf/pacman. Use pip install --break-system-packages if apt lacks the package.
Combine commands with &&."""


def parse_response(raw):
    text = raw.strip()
    if text.startswith("```"):
        text = "\n".join(text.split("\n")[1:])
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            text = text[start:end]
    return json.loads(text)


# --- Agents ---

class Agent:
    def __init__(self, name, backend, color):
        self.name = name
        self.backend = backend  # "claude" or "ollama"
        self.color = color
        self.wallet = 0.0       # earnings
        self.reputation = 50    # 0-100
        self.wins = 0
        self.losses = 0

    def bid(self, command, stderr_text, env_info):
        """Generate a fix and return a bid."""
        prompt = build_prompt(command, stderr_text, env_info)

        log(self.name, self.color, "?", "Generating fix...")
        start = time.time()

        try:
            if self.backend == "claude":
                raw = self._call_claude(prompt)
            else:
                raw = self._call_ollama(prompt)

            elapsed = time.time() - start
            result = parse_response(raw)

            # Build bid
            confidence = float(result.get("confidence", 0.5))
            # Price: Claude charges, Ollama is free
            if self.backend == "claude":
                price = 0.001  # ~$0.001 per call
            else:
                price = 0.0

            bid = {
                "agent": self.name,
                "backend": self.backend,
                "fix": result["fix"],
                "explanation": result.get("explanation", ""),
                "confidence": confidence,
                "price": price,
                "time": elapsed,
                "retry": result.get("retry", True),
            }

            log(self.name, self.color, "$",
                f"Bid: {C_BOLD}{result['fix'][:50]}{C_RESET}")
            log(self.name, self.color, " ",
                f"confidence={confidence:.0%} price=${price:.4f} time={elapsed:.1f}s")

            return bid

        except Exception as e:
            elapsed = time.time() - start
            log(self.name, self.color, "!", f"Failed to bid: {e}")
            return None

    def _call_claude(self, prompt):
        import httpx
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            keyfile = os.path.expanduser("~/.patchdao/api_key")
            if os.path.exists(keyfile):
                with open(keyfile) as f:
                    api_key = f.read().strip()
        resp = httpx.post(
            CLAUDE_API_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={"model": CLAUDE_MODEL, "max_tokens": 1024,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"API {resp.status_code}")
        return resp.json()["content"][0]["text"]

    def _call_ollama(self, prompt):
        import httpx
        resp = httpx.post(
            OLLAMA_URL,
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=120,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Ollama {resp.status_code}")
        return resp.json()["response"]


# --- Registry (The Auctioneer) ---

class Registry:
    """Collects bids, picks winner, verifies, pays out."""

    def score_bid(self, bid):
        """Score a bid. Higher = better.

        Weights: confidence (40%), price (30% inverted), speed (30% inverted)
        """
        if bid is None:
            return -1

        confidence_score = bid["confidence"] * 40

        # Price: free is best. Cap at $0.01
        max_price = 0.01
        price_score = (1 - min(bid["price"], max_price) / max_price) * 30

        # Speed: faster is better. Cap at 30s
        max_time = 30.0
        speed_score = (1 - min(bid["time"], max_time) / max_time) * 30

        return confidence_score + price_score + speed_score

    def pick_winner(self, bids):
        """Select the best bid."""
        scored = [(self.score_bid(b), b) for b in bids if b is not None]
        if not scored:
            return None
        scored.sort(key=lambda x: x[0], reverse=True)

        print()
        log("JUDGE", C_YELLOW, "#", f"{C_BOLD}Scoreboard:{C_RESET}")
        for score, bid in scored:
            log("JUDGE", C_YELLOW, " ",
                f"  {bid['agent']:>6}: score={score:.1f} "
                f"(conf={bid['confidence']:.0%} price=${bid['price']:.4f} "
                f"time={bid['time']:.1f}s)")

        winner_score, winner = scored[0]
        print()
        log("JUDGE", C_YELLOW, "!",
            f"{C_BOLD}Winner: {winner['agent']}{C_RESET} (score {winner_score:.1f})")
        return winner

    def verify(self, fix_cmd, verify_cmd, password=None):
        """Run fix + verification. Returns (success, stdout, stderr)."""
        # Run fix
        if password:
            fix_full = f"echo '{password}' | sudo -S bash -c '{fix_cmd}'"
        else:
            fix_full = fix_cmd

        fix_r = subprocess.run(fix_full, shell=True, capture_output=True,
                               text=True, timeout=120)
        if fix_r.returncode != 0:
            return False, fix_r.stdout, fix_r.stderr

        # Run verification
        verify_r = subprocess.run(verify_cmd, shell=True, capture_output=True,
                                  text=True, timeout=30)
        return verify_r.returncode == 0, verify_r.stdout, verify_r.stderr


# --- Main ---

def main():
    if len(sys.argv) < 2:
        print("Usage: arena.py <command that will fail>")
        print("Example: arena.py 'python3 -c \"import flask\"'")
        return

    command = " ".join(sys.argv[1:])

    print()
    print(f"  {C_BOLD}=== PatchDAO Arena ==={C_RESET}")
    print(f"  Two agents. One job. May the best fix win.")
    print()

    # Step 1: Run the command, capture the error
    log("USER", C_BLUE, ">", f"Running: {command}")
    proc = subprocess.run(command, shell=True, capture_output=True, text=True)

    if proc.returncode == 0:
        print(proc.stdout)
        log("USER", C_BLUE, "+", "Command succeeded. Nothing to fix.")
        return

    stderr = proc.stderr
    log("USER", C_BLUE, "!", f"Failed (exit {proc.returncode})")
    # Show first 3 lines of error
    for line in stderr.strip().splitlines()[:3]:
        log("USER", C_BLUE, " ", f"  {C_DIM}{line}{C_RESET}")

    env_info = get_env_info()

    # Step 2: Post the job
    print()
    log("ARENA", C_MAGENTA, "#",
        f"{C_BOLD}Job posted. Agents bidding...{C_RESET}")
    print()

    # Step 3: Agents bid in parallel
    claude_agent = Agent("Claude", "claude", C_CYAN)
    ollama_agent = Agent("Ollama", "ollama", C_GREEN)

    bids = [None, None]

    def agent_bid(agent, idx):
        bids[idx] = agent.bid(command, stderr, env_info)

    t1 = threading.Thread(target=agent_bid, args=(claude_agent, 0))
    t2 = threading.Thread(target=agent_bid, args=(ollama_agent, 1))

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Step 4: Registry picks winner
    registry = Registry()
    winner = registry.pick_winner([b for b in bids if b])

    if not winner:
        log("ARENA", C_MAGENTA, "!", "No valid bids. Aborting.")
        return

    # Step 5: Show the contract
    print()
    log("ARENA", C_MAGENTA, "#", f"{C_BOLD}Contract:{C_RESET}")
    log("ARENA", C_MAGENTA, " ", f"  Fix:    {winner['fix'][:70]}")
    log("ARENA", C_MAGENTA, " ", f"  Proof:  '{command}' exits 0")
    log("ARENA", C_MAGENTA, " ", f"  Price:  ${winner['price']:.4f}")

    # Step 6: Ask user to approve
    if sys.stdin.isatty():
        print()
        confirm = input(f"  ?  Execute this contract? [Y/n] ")
        if confirm.strip().lower() == "n":
            log("USER", C_BLUE, "-", "Contract rejected.")
            return

    # Step 7: Execute and verify
    print()
    log("ARENA", C_MAGENTA, "*", "Executing contract...")

    password = os.environ.get("SUDO_PASSWORD", "")
    success, stdout, stderr_out = registry.verify(
        winner["fix"], command, password if password else None
    )

    # Step 8: Settle
    print()
    if success:
        winner_agent = claude_agent if winner["backend"] == "claude" else ollama_agent
        winner_agent.wallet += winner["price"]
        winner_agent.wins += 1
        winner_agent.reputation = min(100, winner_agent.reputation + 5)

        loser = ollama_agent if winner["backend"] == "claude" else claude_agent
        loser.losses += 1

        log("ARENA", C_GREEN, "+",
            f"{C_BOLD}Contract SATISFIED.{C_RESET} "
            f"{winner['agent']} earned ${winner['price']:.4f}")

        # Show loser what would have happened
        loser_bid = bids[1] if winner["backend"] == "claude" else bids[0]
        if loser_bid:
            log("ARENA", C_DIM, " ",
                f"({loser.name}'s bid was: {loser_bid['fix'][:50]})")
    else:
        log("ARENA", C_RED, "!",
            f"{C_BOLD}Contract FAILED.{C_RESET} "
            f"No payment. {winner['agent']} loses reputation.")

        winner_agent = claude_agent if winner["backend"] == "claude" else ollama_agent
        winner_agent.reputation = max(0, winner_agent.reputation - 10)
        winner_agent.losses += 1

        if stderr_out:
            for line in stderr_out.strip().splitlines()[:2]:
                log("ARENA", C_RED, " ", f"  {C_DIM}{line}{C_RESET}")

        # Did the loser's fix work?
        loser_bid = bids[1] if winner["backend"] == "claude" else bids[0]
        if loser_bid:
            print()
            log("ARENA", C_YELLOW, "?",
                f"Trying runner-up ({loser_bid['agent']})...")
            s2, _, e2 = registry.verify(
                loser_bid["fix"], command, password if password else None
            )
            if s2:
                loser_agent = ollama_agent if winner["backend"] == "claude" else claude_agent
                loser_agent.wins += 1
                loser_agent.reputation = min(100, loser_agent.reputation + 10)
                log("ARENA", C_GREEN, "+",
                    f"{C_BOLD}Runner-up SUCCEEDED!{C_RESET} "
                    f"{loser_bid['agent']} wins by default.")
            else:
                log("ARENA", C_RED, "!", "Runner-up also failed. No winner.")

    # Final scoreboard
    print()
    log("ARENA", C_MAGENTA, "#", f"{C_BOLD}Final standings:{C_RESET}")
    for agent in [claude_agent, ollama_agent]:
        log("ARENA", C_MAGENTA, " ",
            f"  {agent.name:>6}: wallet=${agent.wallet:.4f} "
            f"rep={agent.reputation} W/L={agent.wins}/{agent.losses}")
    print()


if __name__ == "__main__":
    main()
