# Copyright (c) 2026, Rakshit Sharma and contributors
# For license information, please see license.txt

"""
Dry run of a stock repost.

Walks the stock ledger forward from a date the same way erpnext does in
update_entries_after (stock_ledger.py), but keeps everything in memory.
Nothing is written. The Repost Impact Preview report sits on top of this.

Only plain FIFO / LIFO items. No serial or batch, no moving average.
"""

import json
from copy import deepcopy

import frappe
from erpnext.stock.utils import get_combine_datetime, get_valuation_method
from erpnext.stock.valuation import FIFOValuation, LIFOValuation
from frappe import _
from frappe.model.meta import get_field_precision
from frappe.utils import flt, get_datetime

SLE_FIELDS = [
	"name",
	"item_code",
	"warehouse",
	"posting_date",
	"posting_time",
	"posting_datetime",
	"creation",
	"voucher_type",
	"voucher_no",
	"voucher_detail_no",
	"actual_qty",
	"incoming_rate",
	"outgoing_rate",
	"qty_after_transaction",
	"valuation_rate",
	"stock_value",
	"stock_value_difference",
	"stock_queue",
	"is_adjustment_entry",
	"dependant_sle_voucher_detail_no",
	"serial_no",
	"batch_no",
	"serial_and_batch_bundle",
]


def simulate_repost(item_code, warehouse, posting_date, posting_time, new_rates=None):
	"""
	Replay the ledger of one item + warehouse from the given date and time, without saving.

	new_rates is {voucher_detail_no: rate} for the rows you want to change, for example the
	purchase receipt row that should have come in at 80 instead of 60. Rows that get their cost
	from a changed row (finished goods, transfer target) pick up the new rate on the way.

	Returns one dict per stock ledger entry walked, old and new values side by side.
	"""
	new_rates = dict(new_rates or {})
	precision = get_field_precision(frappe.get_meta("Stock Ledger Entry").get_field("stock_value"))
	start_time = get_combine_datetime(posting_date, posting_time)

	balances = {(item_code, warehouse): balance_before(item_code, warehouse, start_time)}
	sles_to_walk = sles_on_or_after(item_code, warehouse, start_time)
	walked = set()
	result = []

	while sles_to_walk:
		sle = sles_to_walk.pop(0)
		if sle.name in walked:
			continue
		walked.add(sle.name)
		check_sle_is_plain(sle)

		balance = balances[(sle.item_code, sle.warehouse)]
		row = redo_sle(sle, balance, new_rates, precision)
		result.append(row)

		if not row.value_changed or flt(sle.actual_qty) >= 0 or not sle.dependant_sle_voucher_detail_no:
			continue

		# this consumed row feeds some other row (finished good, or the target warehouse of a
		# transfer). that row changes too, so it and everything after it join the walk
		for fed_sle in sles_fed_by(sle):
			new_rates[fed_sle.voucher_detail_no] = rate_for_fed_row(sle, row, fed_sle, new_rates)

			key = (fed_sle.item_code, fed_sle.warehouse)
			if key in balances:
				continue

			balances[key] = balance_before(
				fed_sle.item_code, fed_sle.warehouse, fed_sle.posting_datetime, fed_sle.creation
			)
			sles_to_walk.extend(
				sles_on_or_after(
					fed_sle.item_code, fed_sle.warehouse, fed_sle.posting_datetime, fed_sle.creation
				)
			)
			sles_to_walk.sort(key=lambda d: (get_datetime(d.posting_datetime), get_datetime(d.creation)))

	return result


def redo_sle(sle, balance, new_rates, precision):
	qty = flt(sle.actual_qty)
	queue = queue_class_for(sle.item_code)(deepcopy(balance.queue))
	_, value_before = queue.get_total_stock_and_value()

	incoming_rate = flt(sle.incoming_rate)
	if qty > 0 and sle.voucher_detail_no in new_rates:
		incoming_rate = flt(new_rates[sle.voucher_detail_no])

	if sle.voucher_type == "Stock Reconciliation" and not sle.is_adjustment_entry:
		# reconciliation is a hard reset, whatever was in the queue before it
		balance.qty = flt(sle.qty_after_transaction)
		balance.queue = [[balance.qty, flt(sle.valuation_rate)]]
		reset_value = flt(balance.qty * flt(sle.valuation_rate), precision)
		value_diff = reset_value - balance.value
		balance.value = reset_value
	else:
		if qty > 0:
			queue.add_stock(qty=qty, rate=incoming_rate)
		else:
			queue.remove_stock(
				qty=abs(qty),
				outgoing_rate=flt(sle.outgoing_rate),
				rate_generator=lambda: flt(balance.valuation_rate),
			)
		_, value_after = queue.get_total_stock_and_value()
		value_diff = flt(value_after - value_before, precision)
		balance.queue = queue.state
		balance.qty = flt(balance.qty + qty)
		balance.value = flt(balance.value + value_diff, precision)

	if not balance.qty:
		balance.value = 0.0
	balance.valuation_rate = balance.value / balance.qty if balance.qty else flt(sle.valuation_rate)

	old_diff = flt(sle.stock_value_difference, precision)
	new_diff = flt(value_diff, precision)
	old_queue = json.loads(sle.stock_queue or "[]")

	value_changed = old_diff != new_diff
	balance_changed = flt(sle.stock_value, precision) != balance.value or old_queue != balance.queue
	if value_changed:
		status = "Value changes"
	elif balance_changed:
		status = "Balance only"
	else:
		status = "Unchanged"

	return frappe._dict(
		sle=sle.name,
		posting_datetime=sle.posting_datetime,
		voucher_type=sle.voucher_type,
		voucher_no=sle.voucher_no,
		item_code=sle.item_code,
		warehouse=sle.warehouse,
		actual_qty=qty,
		old_rate=abs(old_diff / qty) if qty else 0,
		new_rate=abs(new_diff / qty) if qty else 0,
		old_diff=old_diff,
		new_diff=new_diff,
		change=flt(new_diff - old_diff, precision),
		old_stock_value=flt(sle.stock_value, precision),
		new_stock_value=balance.value,
		old_queue=json.dumps(old_queue),
		new_queue=json.dumps(balance.queue),
		value_changed=value_changed,
		status=status,
	)


def rate_for_fed_row(consumed_sle, consumed_row, fed_sle, new_rates):
	if fed_sle.item_code == consumed_sle.item_code:
		# transfer. the receiving warehouse gets exactly what left the sending one
		return abs(consumed_row.new_diff) / flt(fed_sle.actual_qty)

	# manufacture or repack. the finished good takes the change in this raw material's cost.
	# consumption diffs are negative, so old minus new comes out as +1000 when cost went up by 1000
	extra_cost = flt(consumed_row.old_diff) - flt(consumed_row.new_diff)
	rate_so_far = flt(new_rates.get(fed_sle.voucher_detail_no, fed_sle.incoming_rate))
	return rate_so_far + extra_cost / flt(fed_sle.actual_qty)


def balance_before(item_code, warehouse, posting_datetime, creation=None):
	sle = frappe.qb.DocType("Stock Ledger Entry")
	query = (
		frappe.qb.from_(sle)
		.select(sle.qty_after_transaction, sle.valuation_rate, sle.stock_value, sle.stock_queue)
		.where((sle.item_code == item_code) & (sle.warehouse == warehouse) & (sle.is_cancelled == 0))
		.orderby(sle.posting_datetime, order=frappe.qb.desc)
		.orderby(sle.creation, order=frappe.qb.desc)
		.limit(1)
	)
	if creation:
		query = query.where(
			(sle.posting_datetime < posting_datetime)
			| ((sle.posting_datetime == posting_datetime) & (sle.creation < creation))
		)
	else:
		query = query.where(sle.posting_datetime < posting_datetime)

	last_sle = query.run(as_dict=True)
	last_sle = last_sle[0] if last_sle else frappe._dict()

	return frappe._dict(
		qty=flt(last_sle.get("qty_after_transaction")),
		value=flt(last_sle.get("stock_value")),
		valuation_rate=flt(last_sle.get("valuation_rate")),
		queue=json.loads(last_sle.get("stock_queue") or "[]"),
	)


def sles_on_or_after(item_code, warehouse, posting_datetime, creation=None):
	sle = frappe.qb.DocType("Stock Ledger Entry")
	query = (
		frappe.qb.from_(sle)
		.select(*[sle[field] for field in SLE_FIELDS])
		.where((sle.item_code == item_code) & (sle.warehouse == warehouse) & (sle.is_cancelled == 0))
		.orderby(sle.posting_datetime, order=frappe.qb.asc)
		.orderby(sle.creation, order=frappe.qb.asc)
	)
	if creation:
		query = query.where(
			(sle.posting_datetime > posting_datetime)
			| ((sle.posting_datetime == posting_datetime) & (sle.creation >= creation))
		)
	else:
		query = query.where(sle.posting_datetime >= posting_datetime)

	return query.run(as_dict=True)


def sles_fed_by(consumed_sle):
	return frappe.get_all(
		"Stock Ledger Entry",
		filters={
			"voucher_type": consumed_sle.voucher_type,
			"voucher_no": consumed_sle.voucher_no,
			"voucher_detail_no": consumed_sle.dependant_sle_voucher_detail_no,
			"actual_qty": (">", 0),
			"is_cancelled": 0,
		},
		fields=SLE_FIELDS,
	)


def queue_class_for(item_code):
	method = get_valuation_method(item_code)
	if method == "LIFO":
		return LIFOValuation
	if method == "Moving Average":
		# TODO moving average needs a different walk, not needed for now
		frappe.throw(
			_("{0} uses Moving Average. This preview only handles FIFO and LIFO items.").format(item_code)
		)
	return FIFOValuation


def check_sle_is_plain(sle):
	if sle.serial_and_batch_bundle or sle.serial_no or sle.batch_no:
		frappe.throw(
			_(
				"{0} in {1} has serial / batch valuation. This preview only handles plain FIFO / LIFO items."
			).format(sle.item_code, sle.warehouse)
		)
