param([Parameter(Mandatory=$true)][string]$Source, [Parameter(Mandatory=$true)][string]$Destination)
$ErrorActionPreference = 'Stop'
$cvWord = $null
$cvDocument = $null
try {
    $cvWord = New-Object -ComObject Word.Application
    $cvWord.Visible = $false
    $cvWord.DisplayAlerts = 0
    $cvWord.AutomationSecurity = 3
    $cvDocument = $cvWord.Documents.Open($Source, $false, $true)
    $cvDocument.ExportAsFixedFormat($Destination, 17)
} finally {
    $cvSaveChanges = 0
    if ($null -ne $cvDocument) { $cvDocument.Close([ref]$cvSaveChanges); [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($cvDocument) }
    if ($null -ne $cvWord) { $cvWord.Quit([ref]$cvSaveChanges); [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($cvWord) }
}
