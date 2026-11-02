# Lets real Excel finish samples/structural_test_B.xlsx: computes every formula result, applies an AutoFilter
# with criteria (hides rows the Excel way), builds a real pivot table, adds a threaded comment, saves.
# Run from the repo root after build_structural_workbooks.py:  powershell -File tests/fixtures/excel_post.ps1
$path = (Resolve-Path "samples/structural_test_B.xlsx").Path
$xl = New-Object -ComObject Excel.Application
$xl.Visible = $false
$xl.DisplayAlerts = $false
try {
    $wb = $xl.Workbooks.Open($path)

    # S-22: a real Excel Table with a totals row (row 7); rows 8-9 continue the pattern below it
    $et = $wb.Worksheets.Item("Excel_Table_Totals")
    $lo = $et.ListObjects.Add(1, $et.Range("A1:C6"), [Type]::Missing, 1)
    $lo.Name = "StockTable"
    $lo.ShowTotals = $true
    $lo.ListColumns.Item("Qty").TotalsCalculation = 1      # xlTotalsCalculationSum

    # S-16: Excel's own AutoFilter, keeping Channel = TV (hides the Radio rows)
    $src = $wb.Worksheets.Item("Filter_Source")
    $src.Range("A1:C13").AutoFilter(2, "TV") | Out-Null

    # S-21: a real pivot table on its own sheet, cache source = Filter_Source!A1:C13
    $pv = $wb.Worksheets.Add([Type]::Missing, $wb.Worksheets.Item($wb.Worksheets.Count))
    $pv.Name = "Pivot"
    $cache = $wb.PivotCaches().Create(1, "Filter_Source!R1C1:R13C3")
    $table = $cache.CreatePivotTable("Pivot!R3C1", "RegionPivot")
    $table.PivotFields("Region").Orientation = 1          # row field
    $table.AddDataField($table.PivotFields("Spend"), "Sum of Spend", -4157) | Out-Null

    # S-28: a modern threaded comment (falls back to a classic note on older Excel)
    $cell = $wb.Worksheets.Item("Footer_Date").Range("B3")
    try { $cell.AddCommentThreaded("this spend was revised, check the invoice") | Out-Null }
    catch { $cell.AddComment("this spend was revised, check the invoice") | Out-Null }

    # S-26: a genuine external link — a formula pointing into another workbook (Excel adds the link part)
    $ext = Join-Path (Split-Path $path) "external_budget.xlsx"
    $eb = $xl.Workbooks.Add()
    $eb.Worksheets.Item(1).Range("A1").Value2 = 500
    $eb.SaveAs($ext, 51)
    $eb.Close($false)
    $wb.Worksheets.Item("Side_Calc").Range("D3").Formula = "='" + (Split-Path $ext) + "\[external_budget.xlsx]Sheet1'!`$A`$1"

    $xl.CalculateFull()
    $wb.Save()
    $wb.Close($false)
    "Excel finished $path"
} finally {
    $xl.Quit()
    [System.Runtime.InteropServices.Marshal]::ReleaseComObject($xl) | Out-Null
}
