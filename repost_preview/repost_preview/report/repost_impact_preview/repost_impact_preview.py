# Copyright (c) 2026, Rakshit Sharma and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import flt

from repost_preview.simulation import simulate_repost


def execute(filters=None):
	filters = frappe._dict(filters or {})
	new_rates = {}

	if filters.purchase_receipt:
		# start the walk at the receipt itself and change the rate on its rows for this item + warehouse
		posting_date, posting_time = frappe.db.get_value(
			"Purchase Receipt", filters.purchase_receipt, ["posting_date", "posting_time"]
		)
		filters.from_date, filters.from_time = posting_date, posting_time

		if filters.new_rate is not None:
			receipt_rows = frappe.get_all(
				"Purchase Receipt Item",
				filters={
					"parent": filters.purchase_receipt,
					"item_code": filters.item_code,
					"warehouse": filters.warehouse,
					"docstatus": 1,
				},
				pluck="name",
			)
			for row_name in receipt_rows:
				new_rates[row_name] = flt(filters.new_rate)

	if not filters.from_date:
		frappe.throw(_("From Date is required"))

	rows = simulate_repost(
		filters.item_code,
		filters.warehouse,
		filters.from_date,
		filters.from_time or "00:00:00",
		new_rates,
	)

	return get_columns(), rows, None, None, get_summary(rows)


def get_columns():
	return [
		{"label": _("Posting"), "fieldname": "posting_datetime", "fieldtype": "Datetime", "width": 150},
		{"label": _("Voucher Type"), "fieldname": "voucher_type", "fieldtype": "Data", "width": 120},
		{
			"label": _("Voucher No"),
			"fieldname": "voucher_no",
			"fieldtype": "Dynamic Link",
			"options": "voucher_type",
			"width": 160,
		},
		{"label": _("Item"), "fieldname": "item_code", "fieldtype": "Link", "options": "Item", "width": 120},
		{
			"label": _("Warehouse"),
			"fieldname": "warehouse",
			"fieldtype": "Link",
			"options": "Warehouse",
			"width": 140,
		},
		{"label": _("Qty"), "fieldname": "actual_qty", "fieldtype": "Float", "width": 80},
		{"label": _("Old Rate"), "fieldname": "old_rate", "fieldtype": "Currency", "width": 100},
		{"label": _("New Rate"), "fieldname": "new_rate", "fieldtype": "Currency", "width": 100},
		{"label": _("Old Value Diff"), "fieldname": "old_diff", "fieldtype": "Currency", "width": 120},
		{"label": _("New Value Diff"), "fieldname": "new_diff", "fieldtype": "Currency", "width": 120},
		{"label": _("Change"), "fieldname": "change", "fieldtype": "Currency", "width": 100},
		{
			"label": _("Old Stock Value"),
			"fieldname": "old_stock_value",
			"fieldtype": "Currency",
			"width": 120,
		},
		{
			"label": _("New Stock Value"),
			"fieldname": "new_stock_value",
			"fieldtype": "Currency",
			"width": 120,
		},
		{"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 110},
		{"label": _("Old Queue"), "fieldname": "old_queue", "fieldtype": "Data", "width": 200},
		{"label": _("New Queue"), "fieldname": "new_queue", "fieldtype": "Data", "width": 200},
	]


def get_summary(rows):
	changed = [row for row in rows if row.status == "Value changes"]
	balance_only = [row for row in rows if row.status == "Balance only"]
	vouchers = {(row.voucher_type, row.voucher_no) for row in changed}

	# sales rows are where cost leaves stock and goes to P&L, so their change is the COGS change.
	# the sum over all rows is whatever is left sitting in stock
	cogs_change = -sum(
		row.change for row in changed if row.voucher_type in ("Delivery Note", "Sales Invoice")
	)
	closing_change = sum(row.change for row in changed)

	return [
		{"label": _("Entries Walked"), "value": len(rows), "datatype": "Int"},
		{"label": _("Value Changes"), "value": len(changed), "datatype": "Int", "indicator": "Red"},
		{"label": _("Balance Only"), "value": len(balance_only), "datatype": "Int", "indicator": "Orange"},
		{"label": _("Vouchers Needing GL Repost"), "value": len(vouchers), "datatype": "Int"},
		{"label": _("Change In Cost Of Goods Sold"), "value": flt(cogs_change), "datatype": "Currency"},
		{"label": _("Change In Closing Stock"), "value": flt(closing_change), "datatype": "Currency"},
	]
