param(
    [string]$TextFile,
    [string]$WavFile,
    [int]$Rate = 0
)
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $s.Rate = $Rate
    $s.SetOutputToWaveFile($WavFile)
    $text = Get-Content -Raw -Encoding UTF8 -LiteralPath $TextFile
    $s.Speak($text)
} finally {
    $s.Dispose()
}
