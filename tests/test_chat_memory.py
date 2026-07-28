from __future__ import annotations

import unittest

from backend.architectos.chat_memory import (
    evaluate_chat_turn,
    extract_durable_facts,
    is_chitchat_user_message,
)


class ChatMemoryGateTests(unittest.TestCase):
    def test_chitchat_detected(self) -> None:
        for text in ("привет", "как дела?", "hello!", "thanks", "ok"):
            self.assertTrue(is_chitchat_user_message(text), text)

    def test_strict_keeps_architecture_decision(self) -> None:
        verdict = evaluate_chat_turn(
            "Architecture decision: use Redis for session cache with 15m TTL.",
            "Agreed. We will use Redis with 15 minute TTL.",
            mode="strict",
        )
        self.assertTrue(verdict.keep)
        self.assertTrue(verdict.facts)
        self.assertIn("Redis", verdict.facts[0]["text"])

    def test_strict_skips_long_polite_reply_to_greeting(self) -> None:
        assistant = "Hello! I'm doing well, thanks for asking. " * 20
        verdict = evaluate_chat_turn("как дела?", assistant, mode="strict")
        self.assertFalse(verdict.keep)
        self.assertEqual(verdict.reason, "chitchat")

    def test_off_mode(self) -> None:
        verdict = evaluate_chat_turn(
            "Architecture decision: never store secrets in git.",
            "Understood.",
            mode="off",
        )
        self.assertFalse(verdict.keep)

    def test_extract_facts_prefers_user_statement(self) -> None:
        facts = extract_durable_facts(
            "Constraint: never commit API keys to the repository.",
            "Okay, I will remember that rule.",
        )
        self.assertTrue(facts)
        self.assertIn("API keys", facts[0]["text"])


if __name__ == "__main__":
    unittest.main()
