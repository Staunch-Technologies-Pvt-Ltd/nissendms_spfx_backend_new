import unittest
from unittest.mock import patch

from app.migration_assistant.graph import term_store

# Mirrors the live "Technical and Crewing" / "Vessel Name" term sets closely
# enough for the cases that broke resolution in production: a singular folder
# name vs a plural term, the same child label under two parents, and a term
# whose stored label has a capital i where the folder has a lowercase L.
TERMS = [
    {"id": "g-drawings", "label": "Drawings", "labels": ["Drawings"], "depth": 0, "path": "Drawings", "ancestors": ()},
    {"id": "c-archive", "label": "Archive", "labels": ["Archive"], "depth": 1, "path": "Drawings > Archive", "ancestors": ("g-drawings",)},
    {"id": "c-elec-dwg", "label": "Electrical", "labels": ["Electrical"], "depth": 1, "path": "Drawings > Electrical", "ancestors": ("g-drawings",)},
    {"id": "g-manuals", "label": "Manuals", "labels": ["Manuals"], "depth": 0, "path": "Manuals", "ancestors": ()},
    {"id": "c-elec-man", "label": "Electrical", "labels": ["Electrical"], "depth": 1, "path": "Manuals > Electrical", "ancestors": ("g-manuals",)},
    {"id": "v-banco", "label": "Maersk EI Banco", "labels": ["Maersk EI Banco"], "depth": 0, "path": "Maersk EI Banco", "ancestors": ()},
    {"id": "v-alias", "label": "Peissy", "labels": ["Peissy", "SS378-PEISSY"], "depth": 0, "path": "Peissy", "ancestors": ()},
]


async def fake_terms(term_set_id, *, site_id=None, force_refresh=False):
    return TERMS


class FindTermTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = patch.object(term_store, "get_term_set_terms", fake_terms)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def find(self, label, **kw):
        return await term_store.find_term("set", term_id=None, label=label, **kw)

    async def test_exact_label(self):
        match = await self.find("Archive")
        self.assertEqual(match["id"], "c-archive")
        self.assertEqual(match["matched_by"], "label")

    async def test_term_id_wins_over_label(self):
        match = await term_store.find_term("set", term_id="g-manuals", label="Drawings")
        self.assertEqual(match["id"], "g-manuals")
        self.assertEqual(match["matched_by"], "id")

    async def test_singular_folder_name_resolves_plural_term(self):
        match = await self.find("Drawing", max_depth=0)
        self.assertEqual(match["id"], "g-drawings")
        self.assertEqual(match["matched_by"], "loose_label")

    async def test_look_alike_characters_resolve(self):
        match = await self.find("Maersk El Banco")
        self.assertEqual(match["id"], "v-banco")

    async def test_alternate_label_resolves(self):
        match = await self.find("ss378-peissy")
        self.assertEqual(match["id"], "v-alias")

    async def test_ambiguous_label_is_not_guessed(self):
        self.assertIsNone(await self.find("Electrical"))

    async def test_ancestor_disambiguates(self):
        self.assertEqual((await self.find("Electrical", ancestor_id="g-drawings"))["id"], "c-elec-dwg")
        self.assertEqual((await self.find("Electrical", ancestor_id="g-manuals"))["id"], "c-elec-man")

    async def test_max_depth_excludes_child_terms(self):
        self.assertIsNone(await self.find("Archive", max_depth=0))

    async def test_missing_term_returns_none(self):
        self.assertIsNone(await self.find("Nonexistent Category"))


if __name__ == "__main__":
    unittest.main()
