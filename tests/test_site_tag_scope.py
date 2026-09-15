import unittest

from app.main import _tags_need_attention


class SiteTagScopeTests(unittest.TestCase):
    def test_vessel_only_is_still_missing_group_and_category(self):
        self.assertTrue(_tags_need_attention({
            "department": "Technical",
            "vessel": "Norse Evolution",
            "group": "",
            "category": "",
        }))

    def test_all_core_tags_are_not_missing(self):
        self.assertFalse(_tags_need_attention({
            "department": "Technical",
            "vessel": "Norse Evolution",
            "group": "Drawing",
            "category": "Electrical",
        }))

    def test_category_placeholder_is_reviewable(self):
        self.assertTrue(_tags_need_attention({
            "department": "Technical",
            "vessel": "Norse Evolution",
            "group": "Drawing",
            "category": "To Be Classified",
        }))


if __name__ == "__main__":
    unittest.main()