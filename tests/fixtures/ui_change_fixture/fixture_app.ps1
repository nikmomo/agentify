<#
.SYNOPSIS
  Agentify UI-change fixture: one business operation ("set profile colour and save") in two layouts.
  Test infrastructure only. See SPEC.md.

.PARAMETER Version   A or B
.PARAMETER StateDir  Directory for profile.json (default: %TEMP%\agentify_fixture)
#>
param(
    [ValidateSet('A','B')][string]$Version = 'A',
    [string]$StateDir = (Join-Path $env:TEMP 'agentify_fixture')
)
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
$profilePath = Join-Path $StateDir 'profile.json'
$colours = @('Red','Green','Blue')

function Save-Profile([string]$colour, [string]$notes) {
    $tmp = "$profilePath.tmp"
    (@{ colour = $colour; notes = $notes; version = $Version } | ConvertTo-Json -Compress) | Set-Content -Path $tmp -Encoding UTF8 -NoNewline
    Move-Item -Force $tmp $profilePath
}

$form = New-Object System.Windows.Forms.Form
$form.Text = "Agentify Fixture v$Version"
$form.Size = New-Object System.Drawing.Size(420, 260)
$form.StartPosition = 'CenterScreen'

$notesLabel = New-Object System.Windows.Forms.Label
$notesLabel.Text = 'Notes'; $notesLabel.Location = '20,20'; $notesLabel.AutoSize = $true
$notes = New-Object System.Windows.Forms.TextBox
$notes.Name = 'NotesBox'; $notes.AccessibleName = 'Notes'; $notes.Location = '120,18'; $notes.Width = 240
$form.Controls.AddRange(@($notesLabel, $notes))

$status = New-Object System.Windows.Forms.Label
$status.Name = 'StatusLabel'; $status.Location = '20,170'; $status.AutoSize = $true

if ($Version -eq 'A') {
    $lbl = New-Object System.Windows.Forms.Label
    $lbl.Text = 'Colour'; $lbl.Location = '20,60'; $lbl.AutoSize = $true
    $combo = New-Object System.Windows.Forms.ComboBox
    $combo.Name = 'ColourCombo'; $combo.AccessibleName = 'Colour'; $combo.DropDownStyle = 'DropDownList'
    $combo.Location = '120,58'; $combo.Width = 160
    $colours | ForEach-Object { [void]$combo.Items.Add($_) }
    $combo.SelectedIndex = 0
    $save = New-Object System.Windows.Forms.Button
    $save.Name = 'SaveButton'; $save.Text = 'Save'; $save.Location = '120,100'
    $status.Text = "Current colour: (unsaved)"
    $save.Add_Click({ Save-Profile $combo.SelectedItem $notes.Text; $status.Text = "Current colour: $($combo.SelectedItem)" })
    $form.Controls.AddRange(@($lbl, $combo, $save, $status))
}
else {
    $prefs = New-Object System.Windows.Forms.Button
    $prefs.Name = 'PreferencesButton'; $prefs.Text = "Preferences..."; $prefs.Location = '120,60'; $prefs.Width = 120
    $status.Text = "Profile colour is (unsaved)"
    $prefs.Add_Click({
        $dlg = New-Object System.Windows.Forms.Form
        $dlg.Text = 'Preferences'; $dlg.Size = New-Object System.Drawing.Size(260, 220); $dlg.StartPosition = 'CenterParent'
        $y = 20; $radios = @()
        foreach ($c in $colours) {
            $r = New-Object System.Windows.Forms.RadioButton
            $r.Text = $c; $r.Name = "Radio$c"; $r.Location = "20,$y"; $r.AutoSize = $true
            $dlg.Controls.Add($r); $radios += $r; $y += 30
        }
        $radios[0].Checked = $true
        $apply = New-Object System.Windows.Forms.Button
        $apply.Name = 'ApplyButton'; $apply.Text = 'Apply'; $apply.Location = "20,$($y + 10)"
        $apply.Add_Click({
            $chosen = ($radios | Where-Object Checked | Select-Object -First 1).Text
            Save-Profile $chosen $notes.Text
            $status.Text = "Profile colour is $chosen"
            $dlg.Close()
        }.GetNewClosure())
        $dlg.Controls.Add($apply)
        [void]$dlg.ShowDialog($form)
    })
    $form.Controls.AddRange(@($prefs, $status))
}

[void]$form.ShowDialog()
