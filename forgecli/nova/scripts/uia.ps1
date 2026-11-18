param([int]$Max = 120, [string]$Window = "")
# Lists the clickable / typeable controls of the foreground window (or the first window whose title
# contains -Window) with Windows UI Automation. One JSON object per line, screen pixel boxes.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type @"
using System; using System.Runtime.InteropServices;
public static class Fg { [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow(); }
"@
$AE = [System.Windows.Automation.AutomationElement]
$root = $null
if ($Window) {
    $cond = [System.Windows.Automation.Condition]::TrueCondition
    foreach ($w in $AE::RootElement.FindAll([System.Windows.Automation.TreeScope]::Children, $cond)) {
        if ($w.Current.Name -like "*$Window*") { $root = $w; break }
    }
} else {
    $root = $AE::FromHandle([Fg]::GetForegroundWindow())
}
if ($null -eq $root) { Write-Output "__NO_WINDOW__"; exit 2 }
@{ window = $root.Current.Name } | ConvertTo-Json -Compress

$wanted = @("Button","Edit","Hyperlink","MenuItem","ListItem","TabItem","CheckBox","RadioButton","ComboBox",
            "TreeItem","Document","SplitButton","DataItem","Text","Image")
$walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
$stack = New-Object System.Collections.Stack
$stack.Push(@($root, 0))
$n = 0; $seen = 0
while ($stack.Count -gt 0 -and $n -lt $Max -and $seen -lt 4000) {
    $item = $stack.Pop(); $el = $item[0]; $depth = $item[1]; $seen++
    try {
        $c = $el.Current
        $type = $c.ControlType.ProgrammaticName -replace '^ControlType\.', ''
        $r = $c.BoundingRectangle
        $visible = -not $c.IsOffscreen -and $r.Width -gt 2 -and $r.Height -gt 2 -and -not [double]::IsInfinity($r.X)
        $name = ($c.Name -replace '\s+', ' ').Trim()
        if ($visible -and $depth -gt 0 -and $wanted -contains $type -and ($name -or $type -eq "Edit" -or $type -eq "Document")) {
            if ($type -ne "Text" -or $name.Length -le 80) {
                $n++
                @{ type = $type; name = $name.Substring(0, [Math]::Min(100, $name.Length)); id = $c.AutomationId;
                   x = [int]$r.X; y = [int]$r.Y; w = [int]$r.Width; h = [int]$r.Height; enabled = $c.IsEnabled } | ConvertTo-Json -Compress
            }
        }
        if ($depth -lt 40) {
            $kids = New-Object System.Collections.ArrayList
            $child = $walker.GetFirstChild($el)
            while ($null -ne $child) { [void]$kids.Add($child); $child = $walker.GetNextSibling($child) }
            for ($i = $kids.Count - 1; $i -ge 0; $i--) { $stack.Push(@($kids[$i], $depth + 1)) }
        }
    } catch { }
}
