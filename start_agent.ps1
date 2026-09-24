# Launches the web UI with settings from .env.local (if present).
# Windows counterpart to start_agent.sh - the .sh script uses .venv/bin/python,
# which does not exist on Windows (the venv puts it in .venv\Scripts).
$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

if (Test-Path .env.local) {
    Get-Content .env.local | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith('#') -and $line.Contains('=')) {
            $name  = $line.Substring(0, $line.IndexOf('=')).Trim()
            $value = $line.Substring($line.IndexOf('=') + 1).Trim()
            if ($name) { Set-Item -Path "env:$name" -Value $value }
        }
    }
    Write-Host "loaded .env.local"
}

& .\.venv\Scripts\python.exe app.py
