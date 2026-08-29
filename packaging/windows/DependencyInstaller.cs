using System;
using System.Diagnostics;
using System.IO;

// Small signed launcher for the auditable PowerShell installer.
internal static class DependencyInstaller {
  public static int Main(string[] args) {
    string script = Path.Combine(AppContext.BaseDirectory, "dependency-installer.ps1");
    if (!File.Exists(script)) { Console.Error.WriteLine("dependency-installer.ps1 is missing"); return 2; }
    var psi = new ProcessStartInfo("powershell.exe") { UseShellExecute=false };
    psi.Arguments = "-NoProfile -ExecutionPolicy Bypass -File \"" + script + "\"";
    foreach (var arg in args) psi.Arguments += " \"" + arg.Replace("\"", "\\\"") + "\"";
    try {
      using var process = Process.Start(psi);
      if (process == null) { Console.Error.WriteLine("Could not start PowerShell"); Console.ReadKey(); return 3; }
      process.WaitForExit();
      if (process.ExitCode != 0) { Console.Error.WriteLine("Dependency installation failed (exit " + process.ExitCode + "). See dependency-install.log"); Console.ReadKey(); }
      return process.ExitCode;
    } catch (Exception error) { Console.Error.WriteLine(error); Console.ReadKey(); return 4; }
  }
}
