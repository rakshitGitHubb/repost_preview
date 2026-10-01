# Copyright (c) 2026, Rakshit Sharma and contributors
# For license information, please see license.txt

import frappe
from erpnext.stock.doctype.delivery_note.test_delivery_note import create_delivery_note
from erpnext.stock.doctype.item.test_item import make_item
from erpnext.stock.doctype.purchase_receipt.test_purchase_receipt import make_purchase_receipt
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, today

from repost_preview.simulation import simulate_repost

WAREHOUSE = "_Test Warehouse - _TC"
OTHER_WAREHOUSE = "_Test Warehouse 1 - _TC"


def day(offset):
	return add_days(today(), offset)


def index_by_voucher(rows):
	return {(row.voucher_no, row.item_code): row for row in rows}


class TestRepostImpactPreview(IntegrationTestCase):
	def setup_milk_case(self):
		# the assignment case. opening 100 @ 50, receipt 50 @ 60, delivery 80, receipt 50 @ 60,
		# manufacture 100 milk into 10 powder, deliver the 10 powder
		milk = make_item(properties={"valuation_method": "FIFO"}).name
		powder = make_item(properties={"valuation_method": "FIFO"}).name

		make_stock_entry(item_code=milk, target=WAREHOUSE, qty=100, rate=50, posting_date=day(-30))
		pr1 = make_purchase_receipt(
			item_code=milk, qty=50, rate=60, warehouse=WAREHOUSE, posting_date=day(-20)
		)
		dn1 = create_delivery_note(
			item_code=milk, qty=80, rate=100, warehouse=WAREHOUSE, posting_date=day(-15)
		)
		pr2 = make_purchase_receipt(
			item_code=milk, qty=50, rate=60, warehouse=WAREHOUSE, posting_date=day(-10)
		)

		manufacture = make_stock_entry(
			item_code=milk,
			source=WAREHOUSE,
			qty=100,
			purpose="Manufacture",
			posting_date=day(-9),
			do_not_save=True,
		)
		manufacture.append(
			"items",
			{
				"item_code": powder,
				"t_warehouse": WAREHOUSE,
				"qty": 10,
				"transfer_qty": 10,
				"uom": "Nos",
				"stock_uom": "Nos",
				"conversion_factor": 1.0,
				"is_finished_item": 1,
			},
		)
		manufacture.save()
		manufacture.submit()

		dn2 = create_delivery_note(
			item_code=powder, qty=10, rate=1200, warehouse=WAREHOUSE, posting_date=day(-5)
		)

		return frappe._dict(
			milk=milk, powder=powder, pr1=pr1, dn1=dn1, pr2=pr2, manufacture=manufacture, dn2=dn2
		)

	def test_rate_change_on_receipt_shows_only_rows_that_used_that_layer(self):
		case = self.setup_milk_case()

		rows = simulate_repost(
			case.milk, WAREHOUSE, case.pr1.posting_date, case.pr1.posting_time, {case.pr1.items[0].name: 80}
		)
		rows_by = index_by_voucher(rows)

		# the receipt itself
		self.assertEqual(rows_by[(case.pr1.name, case.milk)].status, "Value changes")
		self.assertEqual(rows_by[(case.pr1.name, case.milk)].new_diff, 4000)

		# the 80 L delivery took the opening milk at 50, so its cost stays. only the balance on the row moves
		self.assertEqual(rows_by[(case.dn1.name, case.milk)].status, "Balance only")
		self.assertEqual(rows_by[(case.dn1.name, case.milk)].new_diff, -4000)
		self.assertEqual(rows_by[(case.dn1.name, case.milk)].new_stock_value, 5000)

		# second receipt keeps its value, but the two 60 layers dont merge any more
		self.assertEqual(rows_by[(case.pr2.name, case.milk)].status, "Balance only")
		self.assertEqual(rows_by[(case.pr2.name, case.milk)].new_diff, 3000)

		# manufacture is the first entry that eats the 80 rate milk: 20@50 + 50@80 + 30@60
		self.assertEqual(rows_by[(case.manufacture.name, case.milk)].status, "Value changes")
		self.assertEqual(rows_by[(case.manufacture.name, case.milk)].new_diff, -6800)

		# and that carries into the powder and the powder delivery
		self.assertEqual(rows_by[(case.manufacture.name, case.powder)].status, "Value changes")
		self.assertEqual(rows_by[(case.manufacture.name, case.powder)].new_diff, 6800)
		self.assertEqual(rows_by[(case.manufacture.name, case.powder)].new_rate, 680)
		self.assertEqual(rows_by[(case.dn2.name, case.powder)].status, "Value changes")
		self.assertEqual(rows_by[(case.dn2.name, case.powder)].new_diff, -6800)

		# closing milk stays 20 L at 60
		self.assertEqual(rows_by[(case.manufacture.name, case.milk)].new_stock_value, 1200)

	def test_preview_does_not_write_anything(self):
		case = self.setup_milk_case()
		before = frappe.db.get_value(
			"Stock Ledger Entry", {"voucher_no": case.dn2.name, "is_cancelled": 0}, "stock_value_difference"
		)

		simulate_repost(
			case.milk, WAREHOUSE, case.pr1.posting_date, case.pr1.posting_time, {case.pr1.items[0].name: 80}
		)

		after = frappe.db.get_value(
			"Stock Ledger Entry", {"voucher_no": case.dn2.name, "is_cancelled": 0}, "stock_value_difference"
		)
		self.assertEqual(flt(before), flt(after))
		self.assertEqual(flt(after), -5800)

	def test_walk_without_a_change_reports_everything_unchanged(self):
		case = self.setup_milk_case()

		rows = simulate_repost(case.milk, WAREHOUSE, case.pr1.posting_date, case.pr1.posting_time)

		# nothing changed on milk, so the walk never goes into powder
		self.assertEqual(len(rows), 4)
		self.assertEqual({row.status for row in rows}, {"Unchanged"})

	def test_transfer_passes_the_new_rate_to_the_other_warehouse(self):
		item = make_item(properties={"valuation_method": "FIFO"}).name

		pr = make_purchase_receipt(
			item_code=item, qty=10, rate=60, warehouse=WAREHOUSE, posting_date=day(-10)
		)
		transfer = make_stock_entry(
			item_code=item, source=WAREHOUSE, target=OTHER_WAREHOUSE, qty=10, posting_date=day(-8)
		)
		dn = create_delivery_note(
			item_code=item, qty=10, rate=100, warehouse=OTHER_WAREHOUSE, posting_date=day(-5)
		)

		rows = simulate_repost(item, WAREHOUSE, pr.posting_date, pr.posting_time, {pr.items[0].name: 80})
		rows_by = {(row.voucher_no, row.warehouse): row for row in rows}

		self.assertEqual(rows_by[(transfer.name, WAREHOUSE)].new_diff, -800)
		self.assertEqual(rows_by[(transfer.name, OTHER_WAREHOUSE)].new_diff, 800)
		self.assertEqual(rows_by[(transfer.name, OTHER_WAREHOUSE)].new_rate, 80)
		self.assertEqual(rows_by[(dn.name, OTHER_WAREHOUSE)].status, "Value changes")
		self.assertEqual(rows_by[(dn.name, OTHER_WAREHOUSE)].new_diff, -800)
