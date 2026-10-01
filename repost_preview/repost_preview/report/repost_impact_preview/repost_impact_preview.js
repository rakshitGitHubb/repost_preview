// Copyright (c) 2026, Rakshit Sharma and contributors
// For license information, please see license.txt

frappe.query_reports["Repost Impact Preview"] = {
	filters: [
		{
			fieldname: "item_code",
			label: __("Item"),
			fieldtype: "Link",
			options: "Item",
			reqd: 1,
			get_query: () => ({ filters: { is_stock_item: 1 } }),
		},
		{
			fieldname: "warehouse",
			label: __("Warehouse"),
			fieldtype: "Link",
			options: "Warehouse",
			reqd: 1,
			get_query: () => ({ filters: { is_group: 0 } }),
		},
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.month_start(),
		},
		{
			fieldname: "from_time",
			label: __("From Time"),
			fieldtype: "Time",
			default: "00:00:00",
		},
		{
			fieldname: "purchase_receipt",
			label: __("Purchase Receipt"),
			fieldtype: "Link",
			options: "Purchase Receipt",
			description: __("Optional. Anchors the walk at this receipt and changes its rate to New Rate."),
			get_query: () => ({ filters: { docstatus: 1 } }),
		},
		{
			fieldname: "new_rate",
			label: __("New Rate"),
			fieldtype: "Currency",
		},
	],

	formatter: function (value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (column.fieldname === "status" && data) {
			if (data.status === "Value changes") {
				value = `<span style="color: var(--red-600)">${value}</span>`;
			} else if (data.status === "Balance only") {
				value = `<span style="color: var(--orange-600)">${value}</span>`;
			}
		}
		if (column.fieldname === "change" && data && data.change) {
			value = `<b>${value}</b>`;
		}
		return value;
	},
};
