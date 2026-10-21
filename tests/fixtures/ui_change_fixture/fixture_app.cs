// Agentify UI-change fixture as a standalone .exe (same UIA surface as fixture_app.ps1).
// Why: computer use classifies powershell.exe-hosted windows as a terminal and refuses to type
// into them, so benchmarks against computer use need a non-terminal host process.
// Build: scripts/build_fixture.ps1 (uses the .NET Framework csc.exe that ships with Windows).
// Run:   AgentifyFixture.exe -Version A|B -StateDir <dir>
using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Text;
using System.Windows.Forms;

static class FixtureApp
{
#if CRM_DEFAULT
    // AgentifyCRM.exe: the same program, defaulting to the CRM form, so that an agent which (re)launches
    // the executable without arguments (computer use does) gets the CRM, not the colour fixture.
    static string version = "F";
#else
    static string version = "A";
#endif
    static string profilePath;
    static readonly string[] Colours = { "Red", "Green", "Blue" };

    static string Esc(string s)
    {
        var sb = new StringBuilder();
        foreach (char c in s ?? "")
        {
            if (c == '"' || c == '\\') sb.Append('\\').Append(c);
            else if (c < 0x20) sb.AppendFormat("\\u{0:x4}", (int)c);
            else sb.Append(c);
        }
        return sb.ToString();
    }

    static void SaveProfile(string colour, string notes)
    {
        string json = "{\"colour\":\"" + Esc(colour) + "\",\"notes\":\"" + Esc(notes) + "\",\"version\":\"" + version + "\"}";
        string tmp = profilePath + ".tmp";
        File.WriteAllText(tmp, json, new UTF8Encoding(false));
        if (File.Exists(profilePath)) File.Delete(profilePath);
        File.Move(tmp, profilePath);
    }

    [STAThread]
    static void Main(string[] args)
    {
        string stateDir = Path.Combine(Path.GetTempPath(), "agentify_fixture");
        for (int i = 0; i + 1 < args.Length; i++)
        {
            if (args[i].Equals("-Version", StringComparison.OrdinalIgnoreCase)) version = args[++i].ToUpperInvariant();
            else if (args[i].Equals("-StateDir", StringComparison.OrdinalIgnoreCase)) stateDir = args[++i];
        }
        if (version != "A" && version != "B" && version != "F") version = "A";
        Directory.CreateDirectory(stateDir);
        profilePath = Path.Combine(stateDir, "profile.json");
        if (version == "F")
        {
            Application.EnableVisualStyles();
            Application.Run(CrmForm(Path.Combine(stateDir, "records.json")));
            return;
        }

        Application.EnableVisualStyles();
        var form = new Form { Text = "Agentify Fixture v" + version, Size = new Size(420, 260), StartPosition = FormStartPosition.CenterScreen };
        form.Controls.Add(new Label { Text = "Notes", Location = new Point(20, 20), AutoSize = true });
        var notes = new TextBox { Name = "NotesBox", AccessibleName = "Notes", Location = new Point(120, 18), Width = 240 };
        form.Controls.Add(notes);
        var status = new Label { Name = "StatusLabel", Location = new Point(20, 170), AutoSize = true };

        if (version == "A")
        {
            form.Controls.Add(new Label { Text = "Colour", Location = new Point(20, 60), AutoSize = true });
            var combo = new ComboBox { Name = "ColourCombo", AccessibleName = "Colour", DropDownStyle = ComboBoxStyle.DropDownList, Location = new Point(120, 58), Width = 160 };
            combo.Items.AddRange(Colours);
            combo.SelectedIndex = 0;
            var save = new Button { Name = "SaveButton", Text = "Save", Location = new Point(120, 100) };
            status.Text = "Current colour: (unsaved)";
            save.Click += (s, e) =>
            {
                SaveProfile((string)combo.SelectedItem, notes.Text);
                status.Text = "Current colour: " + combo.SelectedItem;
            };
            form.Controls.AddRange(new Control[] { combo, save, status });
        }
        else
        {
            var prefs = new Button { Name = "PreferencesButton", Text = "Preferences...", Location = new Point(120, 60), Width = 120 };
            status.Text = "Profile colour is (unsaved)";
            prefs.Click += (s, e) =>
            {
                var dlg = new Form { Text = "Preferences", Size = new Size(260, 220), StartPosition = FormStartPosition.CenterParent };
                var radios = new RadioButton[Colours.Length];
                int y = 20;
                for (int i = 0; i < Colours.Length; i++)
                {
                    radios[i] = new RadioButton { Text = Colours[i], Name = "Radio" + Colours[i], Location = new Point(20, y), AutoSize = true };
                    dlg.Controls.Add(radios[i]);
                    y += 30;
                }
                radios[0].Checked = true;
                var apply = new Button { Name = "ApplyButton", Text = "Apply", Location = new Point(20, y + 10) };
                apply.Click += (s2, e2) =>
                {
                    string chosen = Colours[0];
                    foreach (var r in radios) if (r.Checked) { chosen = r.Text; break; }
                    SaveProfile(chosen, notes.Text);
                    status.Text = "Profile colour is " + chosen;
                    dlg.Close();
                };
                dlg.Controls.Add(apply);
                dlg.ShowDialog(form);
            };
            form.Controls.AddRange(new Control[] { prefs, status });
        }
        Application.Run(form);
    }

    // ---------------------------------------------------------------- version F: "Agentify CRM"
    // A multi-tab customer form for long-horizon and batch benchmarks. "Add record" appends the record
    // to records.json and clears the form (back to the first tab), like a typical data-entry screen.
    static readonly string[] Countries = { "Australia", "Brazil", "Canada", "France", "Germany", "India",
        "Japan", "Kenya", "Mexico", "Spain", "United Kingdom", "United States" };
    static readonly string[] Plans = { "Basic", "Pro", "Enterprise" };
    static readonly string[] Prefs = { "Email", "Phone", "None" };

    static Form CrmForm(string recordsPath)
    {
        var records = new List<string>();
        var form = new Form { Text = "Agentify CRM", Size = new Size(520, 400), StartPosition = FormStartPosition.CenterScreen };
        var tabs = new TabControl { Name = "RecordTabs", Location = new Point(12, 12), Size = new Size(480, 250) };
        var contact = new TabPage { Text = "Contact", Name = "ContactTab" };
        var account = new TabPage { Text = "Account", Name = "AccountTab" };
        var notesTab = new TabPage { Text = "Notes", Name = "NotesTab" };
        tabs.TabPages.AddRange(new[] { contact, account, notesTab });

        Func<TabPage, string, int, TextBox> field = (page, label, y) =>
        {
            page.Controls.Add(new Label { Text = label, Location = new Point(16, y + 3), AutoSize = true });
            var box = new TextBox { Name = label.Replace(" ", "") + "Box", AccessibleName = label, Location = new Point(140, y), Width = 300 };
            page.Controls.Add(box);
            return box;
        };
        var name = field(contact, "Full name", 20);
        var email = field(contact, "Email", 60);
        var phone = field(contact, "Phone", 100);

        account.Controls.Add(new Label { Text = "Country", Location = new Point(16, 23), AutoSize = true });
        var country = new ComboBox { Name = "CountryCombo", AccessibleName = "Country", DropDownStyle = ComboBoxStyle.DropDownList, Location = new Point(140, 20), Width = 220 };
        country.Items.AddRange(Countries);
        account.Controls.Add(country);
        var planGroup = new GroupBox { Text = "Plan", Name = "PlanGroup", Location = new Point(16, 60), Size = new Size(420, 60) };
        var planRadios = new RadioButton[Plans.Length];
        for (int i = 0; i < Plans.Length; i++)
        {
            planRadios[i] = new RadioButton { Text = Plans[i], Name = "Plan" + Plans[i], Location = new Point(16 + i * 130, 24), AutoSize = true };
            planGroup.Controls.Add(planRadios[i]);
        }
        account.Controls.Add(planGroup);
        account.Controls.Add(new Label { Text = "Contact preference", Location = new Point(16, 143), AutoSize = true });
        var pref = new ComboBox { Name = "PreferenceCombo", AccessibleName = "Contact preference", DropDownStyle = ComboBoxStyle.DropDownList, Location = new Point(140, 140), Width = 220 };
        pref.Items.AddRange(Prefs);
        account.Controls.Add(pref);

        var notes = field(notesTab, "Notes", 20);

        var add = new Button { Name = "AddRecordButton", Text = "Add record", Location = new Point(12, 275), Width = 120 };
        var status = new Label { Name = "StatusLabel", Text = "Saved: (none)", Location = new Point(150, 280), AutoSize = true };
        var count = new Label { Name = "CountLabel", Text = "Records: 0", Location = new Point(12, 315), AutoSize = true };

        Action clear = () =>
        {
            name.Text = ""; email.Text = ""; phone.Text = ""; notes.Text = "";
            country.SelectedIndex = -1; pref.SelectedIndex = -1;
            foreach (var r in planRadios) r.Checked = false;
            tabs.SelectedIndex = 0;
        };
        add.Click += (s, e) =>
        {
            if (name.Text.Trim().Length == 0) { status.Text = "Error: Full name is required"; return; }
            string plan = "";
            foreach (var r in planRadios) if (r.Checked) plan = r.Text;
            records.Add("{\"name\":\"" + Esc(name.Text) + "\",\"email\":\"" + Esc(email.Text) + "\",\"phone\":\"" + Esc(phone.Text)
                + "\",\"country\":\"" + Esc((string)country.SelectedItem ?? "") + "\",\"plan\":\"" + Esc(plan)
                + "\",\"contact_preference\":\"" + Esc((string)pref.SelectedItem ?? "") + "\",\"notes\":\"" + Esc(notes.Text) + "\"}");
            string json = "{\"version\":\"F\",\"records\":[" + string.Join(",", records) + "]}";
            string tmp = recordsPath + ".tmp";
            File.WriteAllText(tmp, json, new UTF8Encoding(false));
            if (File.Exists(recordsPath)) File.Delete(recordsPath);
            File.Move(tmp, recordsPath);
            status.Text = "Saved: " + name.Text;
            count.Text = "Records: " + records.Count;
            clear();
        };
        form.Controls.AddRange(new Control[] { tabs, add, status, count });
        return form;
    }
}
