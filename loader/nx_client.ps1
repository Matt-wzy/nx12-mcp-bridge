# NX_MCP_Loader client — send one command (raw bytes), read response with timeout.
param([string]$Cmd = "ping", [int]$TimeoutSec = 25)
$ErrorActionPreference = "Stop"

$pipe = New-Object System.IO.Pipes.NamedPipeClientStream(
    ".", "nx_mcp_loader",
    [System.IO.Pipes.PipeDirection]::InOut,
    [System.IO.Pipes.PipeOptions]::None)
try {
    $pipe.Connect(6000)
    Write-Output ("CONNECTED")
} catch {
    Write-Output ("CONNECT FAIL: " + $_.Exception.Message)
    exit 1
}

$data = [System.Text.Encoding]::UTF8.GetBytes($Cmd + "`n")
$pipe.Write($data, 0, $data.Length)
$pipe.Flush()
Write-Output ("SENT: " + $Cmd)

# read raw bytes until newline, with timeout
$sb = New-Object System.Text.StringBuilder
$deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSec)
while ([DateTime]::UtcNow -lt $deadline) {
    if ($pipe.CanRead) {
        $b = $pipe.ReadByte()
        if ($b -ge 0) {
            if ($b -eq 10) { break }
            [void]$sb.Append([char]$b)
        } else { break }
    } else { break }
}
$resp = $sb.ToString()
if ($resp.Length -eq 0) { $resp = "(TIMEOUT/EMPTY)" }
Write-Output ("RESP: " + $resp)
$pipe.Dispose()
