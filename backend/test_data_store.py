import unittest

from data_store import (
    column_rebuild_properties,
    unsupported_constraint_names,
)


class DataStoreConstraintTests(unittest.TestCase):
    def test_column_rebuild_properties_accepts_all_selected_catalog_fields(self):
        column_row = (
            "name",
            "text",
            None,
            True,
            "",
            "",
        )

        self.assertEqual(
            column_rebuild_properties(column_row),
            ("text", None, True),
        )

    def test_not_null_constraints_do_not_block_safe_table_rebuilds(self):
        constraints = [
            ("players_pkey", "p"),
            ("players_name_not_null", "n"),
        ]

        self.assertEqual(unsupported_constraint_names(constraints), [])

    def test_other_constraints_still_block_table_rebuilds(self):
        constraints = [
            ("players_name_not_null", "n"),
            ("players_valid_position", "c"),
        ]

        self.assertEqual(
            unsupported_constraint_names(constraints),
            ["players_valid_position"],
        )


if __name__ == "__main__":
    unittest.main()
