#!/usr/bin/env python3
"""escrow.py — Commit-reveal escrow with dispute resolution.

Simulates the PatchDAO payment protocol:
1. Agent commits hash(fix) — proves they have a solution without revealing it
2. User locks payment + deposit into escrow
3. Agent reveals fix
4. Fix executes in sandbox
5. Outcome:
   a) Both agree it worked → agent paid, stakes returned
   b) Both agree it failed → user refunded, agent loses reputation
   c) Dispute → neutral validator replays → liar loses their stake

This prevents:
- User stealing the fix (can't see it until payment is locked)
- User lying about the result (validator replay catches them)
- Agent submitting garbage (validator replay catches them)
"""

import hashlib
import json
import os
import sys
import time

# Colors
C_RESET = "\033[0m"
C_RED = "\033[31m"
C_GREEN = "\033[32m"
C_YELLOW = "\033[33m"
C_BLUE = "\033[34m"
C_MAGENTA = "\033[35m"
C_CYAN = "\033[36m"
C_DIM = "\033[2m"
C_BOLD = "\033[1m"


def log(role, color, icon, msg):
    ts = time.strftime("%H:%M:%S")
    print(f"  {C_DIM}{ts}{C_RESET} {color}[{role:>9}]{C_RESET} {icon}  {msg}")


class Wallet:
    """Simple wallet for the simulation."""
    def __init__(self, name, balance):
        self.name = name
        self.balance = balance

    def __str__(self):
        return f"{self.name}: ${self.balance:.4f}"


class EscrowContract:
    """Holds funds until the contract is resolved.

    States: OPEN → FUNDED → REVEALED → SETTLED
    """

    def __init__(self, job_id, bounty, agent_bond_multiplier=10, user_deposit_multiplier=2):
        self.job_id = job_id
        self.state = "OPEN"
        self.bounty = bounty
        self.agent_bond = bounty * agent_bond_multiplier
        self.user_deposit = bounty * user_deposit_multiplier

        # Participants
        self.user_wallet = None
        self.agent_wallet = None

        # Commit-reveal
        self.fix_hash = None       # hash(fix) — committed by agent
        self.fix_plaintext = None  # revealed after funding

        # Funds held
        self.held_user = 0     # bounty + deposit
        self.held_agent = 0    # bond

        # Result
        self.outcome = None    # "success", "failure", "dispute"

    def agent_commit(self, agent_wallet, fix_text):
        """Phase 1: Agent commits hash(fix) without revealing it."""
        assert self.state == "OPEN"

        self.fix_hash = hashlib.sha256(fix_text.encode()).hexdigest()
        self.fix_plaintext = fix_text  # stored privately, not revealed yet
        self.agent_wallet = agent_wallet

        # Agent locks bond
        if agent_wallet.balance < self.agent_bond:
            raise ValueError(f"Agent can't afford bond (${self.agent_bond:.4f})")

        agent_wallet.balance -= self.agent_bond
        self.held_agent = self.agent_bond

        self.state = "COMMITTED"

        log("ESCROW", C_MAGENTA, "#",
            f"Agent committed: hash={self.fix_hash[:16]}...")
        log("ESCROW", C_MAGENTA, " ",
            f"Agent bond locked: ${self.held_agent:.4f}")
        return self.fix_hash

    def user_fund(self, user_wallet):
        """Phase 2: User locks bounty + deposit. Fix is NOT visible yet."""
        assert self.state == "COMMITTED"

        total = self.bounty + self.user_deposit
        if user_wallet.balance < total:
            raise ValueError(f"User can't afford (${total:.4f})")

        user_wallet.balance -= total
        self.held_user = total
        self.user_wallet = user_wallet

        self.state = "FUNDED"

        log("ESCROW", C_MAGENTA, "$",
            f"User funded: ${self.bounty:.4f} bounty + ${self.user_deposit:.4f} deposit")
        log("ESCROW", C_MAGENTA, " ",
            f"Total in escrow: ${self.held_user + self.held_agent:.4f}")

    def agent_reveal(self):
        """Phase 3: Agent reveals the fix. Verified against committed hash."""
        assert self.state == "FUNDED"

        # Verify the reveal matches the commitment
        revealed_hash = hashlib.sha256(self.fix_plaintext.encode()).hexdigest()
        if revealed_hash != self.fix_hash:
            # Agent tried to swap the fix — slash them
            log("ESCROW", C_RED, "!",
                "FRAUD: Revealed fix doesn't match commitment!")
            self._slash_agent("commitment_mismatch")
            return None

        self.state = "REVEALED"
        log("ESCROW", C_GREEN, ">",
            f"Fix revealed: {self.fix_plaintext[:60]}")
        return self.fix_plaintext

    def settle_success(self):
        """Both agree the fix worked. Agent gets paid."""
        assert self.state == "REVEALED"

        # Agent gets: bounty + their bond back
        self.agent_wallet.balance += self.bounty + self.held_agent
        # User gets: their deposit back (they paid the bounty)
        self.user_wallet.balance += self.user_deposit

        self.held_user = 0
        self.held_agent = 0
        self.outcome = "success"
        self.state = "SETTLED"

        log("ESCROW", C_GREEN, "+",
            f"Settled: agent earned ${self.bounty:.4f}")

    def settle_failure(self):
        """Both agree the fix failed. User refunded, agent loses rep."""
        assert self.state == "REVEALED"

        # User gets: full refund (bounty + deposit)
        self.user_wallet.balance += self.held_user
        # Agent gets: bond back (honest failure, no penalty)
        self.agent_wallet.balance += self.held_agent

        self.held_user = 0
        self.held_agent = 0
        self.outcome = "failure"
        self.state = "SETTLED"

        log("ESCROW", C_YELLOW, "-",
            "Settled: fix failed. User refunded, agent bond returned.")

    def dispute(self, validator_says_works):
        """Dispute resolution: neutral validator replays the fix.

        If validator says it works → user lied → user loses deposit
        If validator says it fails → agent lied → agent loses bond
        """
        assert self.state == "REVEALED"

        if validator_says_works:
            # User was lying — fix actually works
            log("ESCROW", C_RED, "!",
                f"{C_BOLD}VERDICT: User lied.{C_RESET} Fix works on validator.")

            # Agent gets: bounty + bond back + user's deposit (penalty)
            self.agent_wallet.balance += self.bounty + self.held_agent + self.user_deposit
            # User gets: nothing (lost deposit as penalty)
            self.user_wallet.balance += 0

            self.outcome = "dispute_user_lied"
        else:
            # Agent was lying — fix doesn't actually work
            log("ESCROW", C_RED, "!",
                f"{C_BOLD}VERDICT: Agent lied.{C_RESET} Fix fails on validator.")

            # User gets: bounty back + deposit back + agent's bond (penalty)
            self.user_wallet.balance += self.held_user + self.held_agent
            # Agent gets: nothing (lost bond)
            self.agent_wallet.balance += 0

            self.outcome = "dispute_agent_lied"

        self.held_user = 0
        self.held_agent = 0
        self.state = "SETTLED"

    def _slash_agent(self, reason):
        """Agent caught cheating — lose everything."""
        self.user_wallet.balance += self.held_user + self.held_agent
        self.agent_wallet.balance += 0
        self.held_user = 0
        self.held_agent = 0
        self.outcome = f"slashed:{reason}"
        self.state = "SETTLED"


def simulate_scenario(name, fix_works_locally, user_honest, fix_works_on_validator=None):
    """Run a full escrow scenario."""
    if fix_works_on_validator is None:
        fix_works_on_validator = fix_works_locally

    print()
    print(f"  {C_BOLD}{'=' * 60}{C_RESET}")
    print(f"  {C_BOLD}Scenario: {name}{C_RESET}")
    print(f"  {C_DIM}fix_works={fix_works_locally} user_honest={user_honest} "
          f"validator={fix_works_on_validator}{C_RESET}")
    print(f"  {C_BOLD}{'=' * 60}{C_RESET}")
    print()

    # Setup wallets
    user = Wallet("User", 1.0)
    agent = Wallet("Agent", 1.0)
    bounty = 0.05

    log("SETUP", C_DIM, " ", f"{user}")
    log("SETUP", C_DIM, " ", f"{agent}")
    log("SETUP", C_DIM, " ", f"Bounty: ${bounty:.4f} | "
        f"Agent bond: ${bounty * 10:.4f} | User deposit: ${bounty * 2:.4f}")
    print()

    # Phase 1: Agent commits
    contract = EscrowContract("job-001", bounty)
    fix = "sudo apt install -y python3-bottle"
    contract.agent_commit(agent, fix)

    log("STATE", C_DIM, " ", f"{user} | {agent}")
    print()

    # Phase 2: User funds
    contract.user_fund(user)

    log("STATE", C_DIM, " ", f"{user} | {agent}")
    print()

    # Phase 3: Agent reveals
    revealed = contract.agent_reveal()

    # Phase 4: Fix executes (simulated)
    print()
    if fix_works_locally:
        log("SANDBOX", C_BLUE, "+", "Fix executed successfully. Exit code 0.")
    else:
        log("SANDBOX", C_BLUE, "!", "Fix failed. Exit code 1.")

    # Phase 5: User reports result
    print()
    if user_honest:
        user_says_works = fix_works_locally
    else:
        # Dishonest user claims opposite
        user_says_works = not fix_works_locally

    if user_says_works:
        log("USER", C_CYAN, "+", "User confirms: fix worked.")
    else:
        log("USER", C_CYAN, "!", "User claims: fix did NOT work.")

    # Phase 6: Settlement
    print()
    if fix_works_locally and user_says_works:
        # Happy path
        contract.settle_success()
    elif not fix_works_locally and not user_says_works:
        # Honest failure
        contract.settle_failure()
    else:
        # Disagreement → dispute → validator
        log("ESCROW", C_YELLOW, "?",
            "Disagreement detected. Escalating to validator...")
        print()

        # Validator replays in clean env
        log("VALIDATOR", C_MAGENTA, ">",
            "Replaying fix in clean environment...")
        time.sleep(0.5)

        if fix_works_on_validator:
            log("VALIDATOR", C_MAGENTA, "+",
                "Validator result: exit code 0 (fix works)")
        else:
            log("VALIDATOR", C_MAGENTA, "!",
                "Validator result: exit code 1 (fix fails)")

        print()
        contract.dispute(fix_works_on_validator)

    # Final balances
    print()
    log("FINAL", C_BOLD, "$", f"{user}")
    log("FINAL", C_BOLD, "$", f"{agent}")
    log("FINAL", C_BOLD, " ", f"Outcome: {contract.outcome}")

    # Return for testing
    return user.balance, agent.balance, contract.outcome


def main():
    print()
    print(f"  {C_BOLD}PatchDAO Escrow Protocol — Scenario Simulator{C_RESET}")
    print(f"  {C_DIM}Proving that honesty is the dominant strategy.{C_RESET}")

    # Scenario 1: Happy path — fix works, everyone honest
    u, a, o = simulate_scenario(
        "Happy path (fix works, everyone honest)",
        fix_works_locally=True, user_honest=True)
    assert o == "success"

    # Scenario 2: Honest failure — fix doesn't work
    u, a, o = simulate_scenario(
        "Honest failure (fix breaks, everyone honest)",
        fix_works_locally=False, user_honest=True)
    assert o == "failure"

    # Scenario 3: User lies — says fix broke when it worked
    u, a, o = simulate_scenario(
        "User lies (fix works, user claims it broke)",
        fix_works_locally=True, user_honest=False)
    assert o == "dispute_user_lied"

    # Scenario 4: Agent submits garbage that somehow passes locally
    # but user honestly reports success
    # (This is the normal case — agent gets paid)

    # Scenario 5: Fix works locally but fails on validator (env difference)
    u, a, o = simulate_scenario(
        "Edge case: works locally, fails on validator",
        fix_works_locally=True, user_honest=False,
        fix_works_on_validator=False)
    assert o == "dispute_agent_lied"

    # Summary
    print()
    print(f"  {C_BOLD}{'=' * 60}{C_RESET}")
    print(f"  {C_BOLD}Summary: Why honesty is optimal{C_RESET}")
    print(f"  {C_BOLD}{'=' * 60}{C_RESET}")
    print(f"""
  {C_GREEN}Honest user + working fix:{C_RESET}
    User pays $0.05 for a fix. Agent earns $0.05. Everyone happy.

  {C_YELLOW}Honest failure:{C_RESET}
    User refunded. Agent gets bond back. No one punished for trying.

  {C_RED}User lies "it broke":{C_RESET}
    Validator proves it works. User loses $0.10 deposit.
    Lying cost the user 2x more than just paying honestly.

  {C_RED}Agent submits garbage:{C_RESET}
    Validator proves it fails. Agent loses $0.50 bond.
    Scamming cost the agent 10x more than the bounty.

  {C_BOLD}Nash equilibrium: both parties are better off being honest.{C_RESET}
""")


if __name__ == "__main__":
    main()
