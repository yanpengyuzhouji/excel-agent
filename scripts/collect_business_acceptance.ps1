param(
    [Parameter(Mandatory=$true)][string]$SourceRoot,
    [Parameter(Mandatory=$true)][string]$OutputRoot,
    [switch]$Resume
)
$ErrorActionPreference = 'Stop'
$source = (Resolve-Path -LiteralPath $SourceRoot).Path.TrimEnd('\', '/')
$output = [IO.Path]::GetFullPath($OutputRoot).TrimEnd('\', '/')
if ($output -eq $source -or $output.StartsWith($source + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Output must be outside the source tree.'
}
if ((Test-Path -LiteralPath $output) -and -not ($Resume -and (Test-Path -LiteralPath (Join-Path $output 'manifest.json')))) { throw 'Output already exists; use -Resume only for a prior collection.' }
function Open-SharedRead([string]$Path) {
    return [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
}
function Get-SharedHash([string]$Path) {
    $inputStream = Open-SharedRead $Path
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($sha.ComputeHash($inputStream)).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose(); $inputStream.Dispose() }
}
$extensions = @('.xlsx', '.xls', '.xlsm', '.xlsb', '.csv', '.et', '.ett', '.xlt', '.xltx', '.xltm')
$files = @(Get-ChildItem -LiteralPath $source -Recurse -File | Where-Object {
    $_.Extension.ToLowerInvariant() -in $extensions -and -not $_.Name.StartsWith('~$') -and
    -not ($_.Attributes -band [IO.FileAttributes]::ReparsePoint)
} | Sort-Object FullName)
New-Item -ItemType Directory -Path (Join-Path $output 'files') -Force | Out-Null
$unique = @{}
$records = @()
$failures = @()
foreach ($file in $files) {
    try {
        $hash = Get-SharedHash $file.FullName
        $extension = $file.Extension.ToLowerInvariant()
        $signatureBytes = New-Object byte[] 8
        $stream = Open-SharedRead $file.FullName
        try { $read = $stream.Read($signatureBytes, 0, 8) } finally { $stream.Dispose() }
        $signature = [BitConverter]::ToString($signatureBytes, 0, $read)
        $container = if ($signature.StartsWith('50-4B-03-04')) { 'zip' } elseif ($signature.StartsWith('D0-CF-11-E0-A1-B1-1A-E1')) { 'ole' } else { 'other' }
        if (-not $unique.ContainsKey($hash)) {
            $relativeCopy = 'files/' + $hash + $extension
            $target = Join-Path $output $relativeCopy
            if (-not (Test-Path -LiteralPath $target)) {
                $inputStream = Open-SharedRead $file.FullName
                try {
                    $outputStream = [IO.File]::Open($target, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
                    try { $inputStream.CopyTo($outputStream) } finally { $outputStream.Dispose() }
                } finally { $inputStream.Dispose() }
            }
            if ((Get-SharedHash $target) -ne $hash -or (Get-SharedHash $file.FullName) -ne $hash) { throw 'Copy hash mismatch; source may have changed during collection.' }
            $unique[$hash] = $relativeCopy
        }
        $relativeSource = $file.FullName.Substring($source.Length).TrimStart('\', '/')
        $records += [pscustomobject]@{
            sample_id = 'sha256:' + $hash
            source_relative_path = $relativeSource
            source_extension = $extension
            category = ($relativeSource -split '[\\/]')[0]
            source_kind = $(if ($relativeSource -match '^(acceptance-results|data-cleaning-service|parser-regression)') { 'existing_test_or_generated_artifact_review_separately' } else { 'business_candidate' })
            bytes = $file.Length
            modified_utc = $file.LastWriteTimeUtc.ToString('o')
            sha256 = $hash
            collected_file = $unique[$hash]
            container_signature = $container
            current_format_support = $(if ($extension -in '.xlsx', '.xlsm', '.csv') { 'candidate_supported_not_validated' } else { 'unsupported_format_expected_rejection' })
            gold_status = 'pending_independent_business_review'
        }
    } catch {
        $failures += [pscustomobject]@{source_relative_path=$file.FullName.Substring($source.Length).TrimStart('\', '/'); error=$_.Exception.Message}
    }
}
$manifest = [ordered]@{
    schema_version = 1
    collected_at_utc = [DateTime]::UtcNow.ToString('o')
    source_root = $source
    privacy = 'Private original business data. Not anonymized. Do not commit, publish, embed in images, or send to cloud models without approval.'
    validation = 'Collection and copy integrity only; no imports, model calls, answer validation or formula recalculation performed.'
    discovered_files = $files.Count
    collected_source_files = $records.Count
    unique_contents = $unique.Count
    duplicate_source_files = $records.Count - $unique.Count
    formats = @($records | Group-Object source_extension | ForEach-Object { [pscustomobject]@{extension=$_.Name;count=$_.Count} })
    files = $records
    failures = $failures
}
$manifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $output 'manifest.json') -Encoding UTF8
$records | Select-Object sample_id,source_relative_path,source_extension,category,bytes,collected_file,current_format_support,gold_status |
    Export-Csv -LiteralPath (Join-Path $output 'review-index.csv') -NoTypeInformation -Encoding UTF8
$business = @($records | Where-Object { $_.source_kind -eq 'business_candidate' })
$shortlist = @(
    $business | Where-Object { $_.source_extension -in '.xlsx', '.csv', '.xlsm' } | Group-Object category | ForEach-Object {
        $_.Group | Sort-Object bytes | Select-Object -First 1
        $_.Group | Sort-Object bytes -Descending | Select-Object -First 1
    }
    $business | Where-Object { $_.source_relative_path -match '配网项目建议书月度报表2021|2025年采购目录汇总' }
    $business | Group-Object source_extension | ForEach-Object { $_.Group | Sort-Object bytes -Descending | Select-Object -First 2 }
) | Sort-Object sample_id -Unique
@($shortlist | ForEach-Object { [ordered]@{
    sample_id=$_.sample_id;collected_file=$_.collected_file;source_relative_path=$_.source_relative_path
    source_kind=$_.source_kind;format_expectation=$_.current_format_support
    review_status='pending_independent_business_review'
    checks=@('source_sheet_and_business_row_coverage','identifier_and_unit_preservation','independent_count_and_amount_reconciliation','query_evidence_and_answer_consistency')
    question=$null;expected_business_rows=$null;expected_amount=$null;unit=$null;source_coordinates=$null;reviewer=$null
} }) | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $output 'acceptance-candidates.json') -Encoding UTF8
[pscustomobject]@{discovered=$files.Count;collected=$records.Count;unique=$unique.Count;duplicates=$records.Count-$unique.Count;failed=$failures.Count;output=$output} | ConvertTo-Json -Compress
if ($failures.Count) { exit 1 }
