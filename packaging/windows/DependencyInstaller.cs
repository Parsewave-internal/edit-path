using System;
using System.Diagnostics;
using System.IO;

// Small signed launcher for the auditable PowerShell installer.
internal static class DependencyInstaller {
  public static int Main(string[] args) {
    string script = Path.Combine(AppContext.BaseDirectory, "dependency-installer.ps1");
    if (!File.Exists(script)) { Console.Error.WriteLine("dependency-installer.ps1 is missing"); return 2; }
    var psi = new ProcessStartInfo("powershell.exe") { UseShellExecute=false };
    psi.ArgumentList.Add("-NoProfile"); psi.ArgumentList.Add("-ExecutionPolicy"); psi.ArgumentList.Add("Bypass");
    psi.ArgumentList.Add("-File"); psi.ArgumentList.Add(script);
    foreach (var arg in args) psi.ArgumentList.Add(arg);
    using var process = Process.Start(psi); process.WaitForExit(); return process.ExitCode;
  }
}
