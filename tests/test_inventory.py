import sqlite3
import unittest

from inventory import get_listing, init_inventory, search_inventory


class InventoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = sqlite3.connect(":memory:")
        init_inventory(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def test_import_preserves_cleaned_listing_ids(self):
        init_inventory(self.db)
        cars = search_inventory(self.db, limit=100)
        self.assertEqual([c["listing_id"] for c in cars], list(range(1, 101)))
        self.assertEqual(get_listing(self.db, 12)["model"], "3")
        self.assertIsNone(get_listing(self.db, 1000))

    def test_cash_is_distinct_from_finance_and_salary(self):
        for listing_id, expected in [(3, 119750), (12, 35000), (32, 70000), (15, 1349999)]:
            with self.subTest(listing_id=listing_id):
                car = get_listing(self.db, listing_id)
                self.assertEqual(car["price_aed"], expected)
                self.assertIn(car["price_evidence"], car["description"])
        monthly_only = get_listing(self.db, 92)
        self.assertIn("5,805/mo", monthly_only["title"])
        self.assertIsNone(monthly_only["price_aed"])
        self.assertIsNone(get_listing(self.db, 6)["price_aed"])  # Unlabelled "30500".

    def test_filters_omit_unknowns_and_handle_inclusive_bounds(self):
        self.assertEqual([c["listing_id"] for c in search_inventory(self.db, max_price=25000)], [20])
        self.assertEqual(search_inventory(self.db, max_price=21499), [])
        zero_km = search_inventory(self.db, max_mileage=0)
        self.assertEqual({c["listing_id"] for c in zero_km}, {28, 54, 66})
        bounded = search_inventory(self.db, min_year=2018, max_year=2018, min_price=119750, max_price=119750)
        self.assertEqual([c["listing_id"] for c in bounded], [3])

    def test_service_intervals_and_top_speeds_are_not_mileage(self):
        for listing_id in (17, 22, 38, 65):  # ID 65 says only "71000 KM Warranty".
            self.assertIsNone(get_listing(self.db, listing_id)["mileage_km"])
        for listing_id, km in ((42, 23900), (68, 34927), (92, 23900)):
            self.assertEqual(get_listing(self.db, listing_id)["mileage_km"], km)

    def test_search_returns_source_and_orders_results_consistently(self):
        cars = search_inventory(self.db, query="panoramic roof", limit=100)
        self.assertTrue(cars)
        self.assertEqual(cars, search_inventory(self.db, query="PANORAMIC roof", limit=100))
        for car in cars:
            text = (car["title"] + " " + car["description"]).lower()
            self.assertIn("panoramic", text)
            self.assertIn("roof", text)
        self.assertEqual(search_inventory(self.db, make="Ford' OR 1=1 --"), [])
        self.assertEqual(search_inventory(self.db, make="VW", model="Beetle")[0]["listing_id"], 60)

    def test_attributes_only_use_wording_supported_by_listing(self):
        self.assertEqual(get_listing(self.db, 92)["color"], "white")
        self.assertIsNone(get_listing(self.db, 42)["color"])  # Black Badge is a trim.
        self.assertIsNone(get_listing(self.db, 72)["color"])  # White/black interior.
        self.assertEqual(search_inventory(self.db, color="Gray", make="Volvo")[0]["listing_id"], 96)
        suvs = search_inventory(self.db, body_style="SUV", limit=100)
        self.assertTrue(suvs)
        self.assertTrue(all("suv" in c["body_style_evidence"].lower() for c in suvs))

    def test_all_extracted_values_link_to_source_text(self):
        for car in search_inventory(self.db, limit=100):
            for value, proof in (("price_aed", "price_evidence"), ("mileage_km", "mileage_evidence"),
                                 ("color", "color_evidence"), ("body_style", "body_style_evidence")):
                evidence = car[proof]
                with self.subTest(listing=car["listing_id"], field=value):
                    if car[value] is None:
                        self.assertIsNone(evidence)
                    else:
                        self.assertTrue(evidence)
                        self.assertTrue(evidence in car["title"] or evidence in car["description"])


if __name__ == "__main__":
    unittest.main()
