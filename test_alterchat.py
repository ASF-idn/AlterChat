"""
Unit tests for AlterChat DataManager, Parser, RAG memory system, and AI Engine.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

import data_manager as dm


SAMPLE_WA_CHAT = """12/01/23, 10.15 - Pesan dan panggilan terenkripsi secara end-to-end.
12/01/23, 10.16 - Rizky: P bro, lagi dimana?
12/01/23, 10.16 - Rizky: Jadi nongkrong ga ntar malem di kopi kenangan?
12/01/23, 10.18 - Saya: Lagi di kantor nih bro, baru kelar meeting.
12/01/23, 10.18 - Saya: Jadi dong, jam berapaan?
12/01/23, 10.20 - Rizky: <Media tidak disertakan>
12/01/23, 10.21 - Rizky: Jam 8 malem aja santuyy wkwkwk
12/01/23, 10.21 - Rizky: Ajak si Dimas sekalian kalo dia mau ikut
12/01/23, 10.22 - Saya: Oke siap, ntar gua chat Dimas dulu ya.
12/01/23, 10.25 - Rizky: Sip mantull, kabarin yaa
12/01/23, 10.26 - Rizky: Pesan ini telah dihapus
12/01/23, 10.27 - Rizky: Oiya bawa laptop lu sekalian bro, mau minta tolong cek codingan python
12/01/23, 10.30 - Saya: Aman, nanti gua bawa.
"""


class TestWhatsAppParser(unittest.TestCase):
    def test_parse_and_sender_separation(self):
        parsed = dm.parse_whatsapp_text(SAMPLE_WA_CHAT)

        # Ensure system message and noise were filtered
        self.assertEqual(parsed.system_messages, 1)
        self.assertGreater(parsed.skipped_messages, 0)  # Media & deleted message

        # Verify sender counts
        self.assertIn("Rizky", parsed.sender_counts)
        self.assertIn("Saya", parsed.sender_counts)

        # Rizky should have 6 valid messages:
        self.assertEqual(parsed.sender_counts["Rizky"], 6)
        self.assertEqual(parsed.sender_counts["Saya"], 4)

    def test_sender_matching(self):
        senders = ["Rizky Pratama", "Saya Sendiri"]
        match = dm.match_sender("Rizky", senders)
        self.assertEqual(match, "Rizky Pratama")

    def test_persona_building(self):
        parsed = dm.parse_whatsapp_text(SAMPLE_WA_CHAT)
        persona = dm.build_persona("Rizky", "Rizky", parsed)

        # Persona prompt must contain Rizky's name and slang
        self.assertIn("Rizky", persona.system_prompt)
        self.assertTrue(len(persona.few_shots) > 0)
        self.assertEqual(persona.user_sender, "Saya")
        self.assertEqual(persona.stats["friend_messages"], 6)


class TestRAGMemory(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.dm = dm.DataManager()
        self.dm.data_dir = Path(self.tmp_dir)
        self.dm.history_dir = Path(self.tmp_dir) / "history"
        self.dm.avatar_dir = Path(self.tmp_dir) / "avatars"
        self.dm.contacts_file = Path(self.tmp_dir) / "contacts.json"
        self.dm.settings_file = Path(self.tmp_dir) / "settings.json"
        self.dm.ensure_data_structure()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_rag_retrieval(self):
        parsed = dm.parse_whatsapp_text(SAMPLE_WA_CHAT)
        contact = self.dm.create_contact("Rizky", "Rizky", parsed)

        # Simulate user asking about laptop or python
        snippets = self.dm.retrieve_context(contact, "laptop codingan python")
        self.assertTrue(len(snippets) > 0)
        found_topic = any("laptop" in s.lower() or "python" in s.lower() for s in snippets)
        self.assertTrue(found_topic)

        # Test prompt messages assembly
        messages = self.dm.build_prompt_messages(contact, "Bro bawa laptop ga?")
        self.assertTrue(len(messages) >= 2)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[-1]["role"], "user")
        self.assertEqual(messages[-1]["content"], "Bro bawa laptop ga?")


class TestAIEngine(unittest.TestCase):
    def test_tsundere_system_instruction(self):
        import ai_engine as ai

        instruction = ai.build_system_instruction("Rizky")
        self.assertIn("Rizky", instruction)
        self.assertIn("tsundere", instruction)
        self.assertIn("ketus", instruction)
        self.assertIn("gengsi", instruction)
        self.assertIn("bahasa Indonesia gaul", instruction)

    def test_guarded_messages_with_raw_context(self):
        import ai_engine as ai

        raw = [
            {
                "role": "system",
                "content": (
                    "## Real messages Rizky has sent\n"
                    "- \"P bro, lagi dimana?\"\n"
                    "## Memories from past chats (use only if relevant)\n"
                    "Friend: Lagi di kantor nih bro"
                ),
            },
            {"role": "user", "content": "P"},
        ]
        guarded = ai.build_guarded_messages(raw, "Rizky")

        # System message at index 0 combines Tsundere instruction + raw context
        self.assertEqual(guarded[0]["role"], "system")
        sys_text = guarded[0]["content"]
        self.assertIn("Kamu adalah Rizky", sys_text)
        self.assertIn("tsundere", sys_text)
        self.assertIn("P bro, lagi dimana?", sys_text)
        self.assertIn("Lagi di kantor nih bro", sys_text)

        # User message preserved at the end
        self.assertEqual(guarded[-1]["role"], "user")
        self.assertEqual(guarded[-1]["content"], "P")

    def test_engine_singleton(self):
        import ai_engine as ai

        engine1 = ai.get_engine()
        engine2 = ai.get_engine()
        self.assertIs(engine1, engine2)


if __name__ == "__main__":
    unittest.main()
