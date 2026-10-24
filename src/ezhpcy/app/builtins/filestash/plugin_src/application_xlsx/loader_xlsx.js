// Spreadsheets (xlsx, xls, ods) in Filestash's table viewer, read by SheetJS.
//
// The table viewer gives search, sorting and scrolling; this gives it the rows.
// Columns are named as the spreadsheet's are (A, B, …), after the row's number,
// and, for a workbook of several sheets, the sheet's name.
import { loadJS } from "../../helpers/loader.js";

await loadJS(import.meta.url, "./lib/vendor/xlsx.full.min.js");

export default async function(ITable) {
    return class SpreadsheetTable extends ITable {
        constructor(response) {
            super();
            const XLSX = window.XLSX;
            const workbook = XLSX.read(response, { type: "array", dense: true });
            const withSheet = workbook.SheetNames.length > 1;
            let width = 0;
            this.rows = [];
            for (const sheetName of workbook.SheetNames) {
                const sheet = workbook.Sheets[sheetName];
                if (!sheet["!ref"]) continue;
                const first = XLSX.utils.decode_range(sheet["!ref"]).s.r;
                // Formatted as the spreadsheet shows them, with empty cells kept.
                const rows = XLSX.utils.sheet_to_json(sheet, {
                    header: 1, raw: false, defval: "", blankrows: false, rawNumbers: false,
                });
                rows.forEach((cells, i) => {
                    const row = { "#": String(first + i + 1) };
                    if (withSheet) row.Sheet = sheetName;
                    cells.forEach((value, column) => {
                        row[XLSX.utils.encode_col(column)] = String(value);
                    });
                    width = Math.max(width, cells.length);
                    this.rows.push(row);
                });
            }
            const names = [
                ...(withSheet ? ["Sheet"] : []),
                "#",
                ...Array.from({ length: width }, (_, column) => XLSX.utils.encode_col(column)),
            ];
            this.header = names.map((name) => ({
                name,
                size: this.rows.reduce((size, row) => Math.max(size, (row[name] || "").length), name.length),
            }));
        }

        getHeader() {
            return this.header;
        }

        getBody() {
            return this.rows;
        }
    };
}
