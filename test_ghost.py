#!/usr/bin/env python3
"""Playwright E2E test for Ghost Chat — WebRTC P2P encrypted chat.

Opens two browser tabs, creates a room as alice, joins as bob,
exchanges WebRTC offer/answer codes, then verifies bidirectional messaging.
"""

import sys
import time
from playwright.sync_api import sync_playwright

URL = "http://localhost:8090/ghost.html"
TIMEOUT = 15000  # ms — generous for WebRTC ICE gathering


def main():
    results = []

    def step(name, ok, detail=""):
        status = "PASS" if ok else "FAIL"
        results.append((name, ok))
        print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()

        # ── Two pages ──────────────────────────────────────────
        page1 = context.new_page()  # alice
        page2 = context.new_page()  # bob

        # ── Page 1: alice creates room ─────────────────────────
        print("\n=== Page 1: Alice creates room ===")
        page1.goto(URL)
        page1.wait_for_selector("#nick-in", timeout=TIMEOUT)
        page1.fill("#nick-in", "alice")
        page1.click("#btn-create")
        step("Alice clicked Create Room", True)

        # Wait for the offer code to be generated (replaces "generating...")
        page1.wait_for_function(
            """() => {
                const ta = document.getElementById('my-code');
                return ta && ta.value && ta.value.startsWith('O:');
            }""",
            timeout=TIMEOUT,
        )
        offer_code = page1.input_value("#my-code")
        step("Offer code generated", offer_code.startswith("O:"), f"{len(offer_code)} chars")

        # Get the room URL hash from page1
        page1_url = page1.url
        room_hash = page1_url.split("#")[1] if "#" in page1_url else ""
        step("Room hash extracted", bool(room_hash), room_hash[:40] + "...")

        # ── Page 2: bob joins via URL hash ─────────────────────
        print("\n=== Page 2: Bob joins room ===")
        page2.goto(f"{URL}#{room_hash}")
        page2.wait_for_selector("#host-code", timeout=TIMEOUT)

        # Set bob's nickname via localStorage before the page loaded,
        # but the join page already read it. Let's check if nick-in exists;
        # if not, the auto-join flow already started.
        # The doJoin flow reads nick from localStorage. Set it there.
        page2.evaluate("localStorage.setItem('ghost-nick', 'bob')")
        # Reload so it picks up the nick
        page2.goto(f"{URL}#{room_hash}")
        page2.wait_for_selector("#host-code", timeout=TIMEOUT)
        step("Bob's join page loaded", True)

        # Paste the offer code
        page2.fill("#host-code", offer_code)
        page2.click("#btn-process")
        step("Bob pasted offer and clicked Generate Response", True)

        # Wait for answer code
        page2.wait_for_selector("#answer-section:not(.hidden)", timeout=TIMEOUT)
        page2.wait_for_function(
            """() => {
                const ta = document.getElementById('my-answer');
                return ta && ta.value && ta.value.startsWith('A:');
            }""",
            timeout=TIMEOUT,
        )
        answer_code = page2.input_value("#my-answer")
        step("Answer code generated", answer_code.startswith("A:"), f"{len(answer_code)} chars")

        # ── Page 1: alice accepts the answer ───────────────────
        print("\n=== Page 1: Alice connects ===")
        page1.fill("#peer-code", answer_code)
        page1.click("#btn-connect")
        step("Alice pasted answer and clicked Connect", True)

        # Wait for chat view to appear on BOTH pages
        page1.wait_for_selector("#chat-view", timeout=TIMEOUT)
        step("Alice's chat view appeared", True)

        page2.wait_for_selector("#chat-view", timeout=TIMEOUT)
        step("Bob's chat view appeared", True)

        # Small delay to let data channel stabilize
        time.sleep(1)

        # ── Alice sends a message ──────────────────────────────
        print("\n=== Messaging ===")
        page1.wait_for_selector("#msg-in", timeout=TIMEOUT)
        page1.fill("#msg-in", "hello from alice")
        page1.click("#btn-send")
        step("Alice sent message", True)

        # Wait for it to appear on bob's side
        page2.wait_for_function(
            """() => {
                const msgs = document.getElementById('messages');
                return msgs && msgs.innerText.includes('hello from alice');
            }""",
            timeout=TIMEOUT,
        )
        step("Bob received 'hello from alice'", True)

        # ── Bob sends a message ────────────────────────────────
        page2.fill("#msg-in", "hello from bob")
        page2.click("#btn-send")
        step("Bob sent message", True)

        # Wait for it on alice's side
        page1.wait_for_function(
            """() => {
                const msgs = document.getElementById('messages');
                return msgs && msgs.innerText.includes('hello from bob');
            }""",
            timeout=TIMEOUT,
        )
        step("Alice received 'hello from bob'", True)

        # ── Results ────────────────────────────────────────────
        print("\n" + "=" * 50)
        passed = sum(1 for _, ok in results if ok)
        total = len(results)
        print(f"Results: {passed}/{total} passed")
        if passed == total:
            print("ALL TESTS PASSED")
        else:
            print("SOME TESTS FAILED")
            for name, ok in results:
                if not ok:
                    print(f"  FAILED: {name}")

        browser.close()

    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    sys.exit(main())
