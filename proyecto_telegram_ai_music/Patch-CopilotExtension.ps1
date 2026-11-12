<#
.SYNOPSIS
    Applies two patches to VS Code to fix Copilot chat session issues.

.DESCRIPTION
    PATCH 1 — Expired image attachment URLs (extension.js)
    -------------------------------------------------------
    When a Copilot chat session contains images, VS Code stores a temporary GitHub
    attachment URL (https://github.com/github-copilot/chat/attachments/{uuid}) in the
    session JSONL. These URLs expire on GitHub's servers. When the session is resumed
    and a new message is sent, the extension includes the expired URL in the API
    request body. GitHub's backend returns HTTP 400 ("vision_attachment_not_accessible"
    or "attachment not found") which kills the entire request.

    Fix: patch extension.js to skip expired GitHub attachment URLs before they are
    included in the API request. Equivalent TypeScript change in agentPrompt.tsx:

        } else if (part.type === Raw.ChatCompletionContentPartKind.Image) {
    +       if (part.imageUrl.url.startsWith('https://github.com/github-copilot/chat/attachments/')) {
    +           return undefined;
    +       }
            return <HistoricalImage src={part.imageUrl.url} ... />;

    PATCH 2 — 50-session history limit (workbench.desktop.main.js)
    ---------------------------------------------------------------
    VS Code's ChatSessionStore trims the chat history index to 50 sessions on every
    shutdown (chatSessionStore.ts: const maxPersistedSessions = 50). Sessions beyond
    the 50 most-recent are permanently deleted from the index and cannot be recovered
    from the chat history panel, even if their JSONL files still exist on disk.

    Fix: patch workbench.desktop.main.js to raise the limit to 500.

    Both patches use regex so the changing minified variable names don't matter.
    Re-run this script after each VS Code update if the issues reappear.

.PARAMETER ExtensionJsPath
    Optional. Full path to the Copilot extension.js to patch. If not specified, the
    script auto-detects the current VS Code installation.

.PARAMETER WorkbenchJsPath
    Optional. Full path to workbench.desktop.main.js to patch. If not specified, the
    script auto-detects the current VS Code installation.

.NOTES
    Source fixes:
      Patch 1: c:\GIT\vscode\extensions\copilot\src\extension\prompts\node\agent\agentPrompt.tsx
      Patch 2: c:\GIT\vscode\src\vs\workbench\contrib\chat\common\model\chatSessionStore.ts

.EXAMPLE
    .\Patch-CopilotExtension.ps1

.EXAMPLE
    .\Patch-CopilotExtension.ps1 -ExtensionJsPath "C:\Users\Me\.vscode\extensions\github.copilot-chat-0.50.0\dist\extension.js"
#>
param(
  [string]$ExtensionJsPath,
  [string]$WorkbenchJsPath
)

#region --- Write-Log snippet ---
function Write-Log {
  param(
    [Parameter(Mandatory)][string]$Message,
    [ValidateSet('Info', 'Warning', 'Error', 'Success')][string]$Level = 'Info'
  )
  $colour = @{ Info = 'Cyan'; Warning = 'Yellow'; Error = 'Red'; Success = 'Green' }[$Level]
  $prefix = @{ Info = 'INFO'; Warning = 'WARN'; Error = 'ERRO'; Success = 'DONE' }[$Level]
  Write-Host "[$prefix] $Message" -ForegroundColor $colour
}
#endregion

#region --- Constants ---
$githubAttachmentPrefix  = 'https://github.com/github-copilot/chat/attachments/'
$vscodeBases             = @(
  "C:\Users\$env:USERNAME\AppData\Local\Programs\Microsoft VS Code",
  "C:\Users\$env:USERNAME\AppData\Local\Programs\Microsoft VS Code Insiders"
)
$userExtBases            = @(
  "C:\Users\$env:USERNAME\.vscode\extensions",
  "C:\Users\$env:USERNAME\.vscode-insiders\extensions"
)
$maxSessionsOld          = 50
$maxSessionsNew          = 500

# Regex: matches the Image branch one-liner regardless of minified variable names.
# The loop variable itself is captured (group 1) via backreference since it varies between
# builds (was 't', later 'n', etc.) — e.g. if(n.type===za.Raw...Image)return vscpp(coe,{src:n.imageUrl.url,...})
$imageUrlPattern = [regex]'(if\((\w+)\.type===\w+\.Raw\.ChatCompletionContentPartKind\.Image\)return vscpp\(\w+,\{src:\2\.imageUrl\.url,detail:\2\.imageUrl\.detail,mimeType:\2\.imageUrl\.mediaType\}\))'

# Regex: matches the maxPersistedSessions declaration regardless of minified variable names.
# e.g. in VS Code 1.125.x:  var fFo=50,Kut="chat.ChatSessionStore.index"
# The storage key string is used as a stable anchor since it never changes.
$sessionLimitPattern = [regex]'var (\w+)=50,(\w+)="chat\.ChatSessionStore\.index"'
#endregion

#region --- Helper: auto-detect extension.js ---
function Find-ExtensionJs {
  Write-Log "Auto-detecting VS Code Copilot extension.js..."

  $candidates = @()

  # Built-in extension in VS Code (stable + Insiders) install dirs
  foreach ($base in $vscodeBases) {
    if (Test-Path $base) {
      Get-ChildItem $base -Directory | Where-Object { $_.Name -ne 'bin' } | ForEach-Object {
        $path = Join-Path $_.FullName "resources\app\extensions\copilot\dist\extension.js"
        if (Test-Path $path) { $candidates += $path }
      }
    }
  }

  # User-installed copilot-chat extension (stable + Insiders profiles)
  foreach ($base in $userExtBases) {
    if (Test-Path $base) {
      Get-ChildItem $base -Directory | Where-Object { $_.Name -like "github.copilot-chat*" } | ForEach-Object {
        $path = Join-Path $_.FullName "dist\extension.js"
        if (Test-Path $path) { $candidates += $path }
      }
    }
  }

  if ($candidates.Count -eq 0) {
    Write-Log "Could not find extension.js. Use -ExtensionJsPath to specify." Error
    return $null
  }

  return ($candidates | ForEach-Object { Get-Item $_ } | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
}
#endregion

#region --- Helper: auto-detect workbench.desktop.main.js ---
function Find-WorkbenchJs {
  Write-Log "Auto-detecting VS Code workbench.desktop.main.js..."

  $candidates = @()

  # VS Code (stable + Insiders) install dirs
  foreach ($base in $vscodeBases) {
    if (Test-Path $base) {
      Get-ChildItem $base -Directory | Where-Object { $_.Name -ne 'bin' } | ForEach-Object {
        # VS Code stores it at: resources\app\out\vs\workbench\workbench.desktop.main.js
        $path = Join-Path $_.FullName "resources\app\out\vs\workbench\workbench.desktop.main.js"
        if (Test-Path $path) { $candidates += $path }
      }
    }
  }

  if ($candidates.Count -eq 0) {
    Write-Log "Could not find workbench.desktop.main.js. Use -WorkbenchJsPath to specify." Error
    return $null
  }

  return ($candidates | ForEach-Object { Get-Item $_ } | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
}
#endregion

#region --- Helper: update product.json checksum after patching ---
function Update-ProductJsonChecksum {
  param([string]$PatchedFilePath)

  # Walk up the directory tree from the patched file to find product.json
  $dir        = Split-Path $PatchedFilePath -Parent
  $appRoot    = $null
  $productJsonPath = $null
  while ($dir -and $dir -ne (Split-Path $dir -Parent)) {
    $candidate = Join-Path $dir 'product.json'
    if (Test-Path $candidate) {
      $productJsonPath = $candidate
      $appRoot         = $dir
      break
    }
    $dir = Split-Path $dir -Parent
  }

  if (-not $productJsonPath) {
    Write-Log "Could not find product.json — VS Code integrity warning will appear." Warning
    return
  }

  # Derive the checksum key: path relative to the 'out' subdirectory of the app root,
  # using forward slashes (VS Code's convention for checksum keys).
  $outRoot = Join-Path $appRoot 'out'
  if (-not $PatchedFilePath.StartsWith($outRoot, [StringComparison]::OrdinalIgnoreCase)) {
    Write-Log "Patched file is not under 'out/' — no checksum entry expected, skipping." Info
    return
  }
  $checksumKey = $PatchedFilePath.Substring($outRoot.Length).TrimStart('\').Replace('\', '/')

  # Confirm the key exists in product.json
  $productContent = [System.IO.File]::ReadAllText($productJsonPath)
  if ($productContent -notmatch [regex]::Escape("`"$checksumKey`"")) {
    Write-Log "Key '$checksumKey' not found in product.json checksums — skipping." Info
    return
  }

  # Compute SHA256 → base64 (VS Code's hash format)
  $sha256     = [System.Security.Cryptography.SHA256]::Create()
  $fileBytes  = [System.IO.File]::ReadAllBytes($PatchedFilePath)
  $newHash    = [Convert]::ToBase64String($sha256.ComputeHash($fileBytes))
  $sha256.Dispose()

  # Replace the old base64 hash value for this key (preserves all other formatting)
  $pattern    = '("' + [regex]::Escape($checksumKey) + '"\s*:\s*")([A-Za-z0-9+/]+=*)(")'
  $replacement = "`$1$newHash`$3"
  $newContent = [regex]::Replace($productContent, $pattern, $replacement)

  if ($newContent -eq $productContent) {
    Write-Log "Checksum replacement had no effect for '$checksumKey' — skipping." Warning
    return
  }

  [System.IO.File]::WriteAllText($productJsonPath, $newContent, [System.Text.Encoding]::UTF8)
  Write-Log "Updated checksum in product.json for: $checksumKey" Success
}
#endregion

#region --- Patch 1: expired image attachment URLs in extension.js ---
function Invoke-ImageUrlPatch {
  param([string]$TargetPath)

  Write-Host ""
  Write-Log "=== PATCH 1: Expired image attachment URLs ===" Info

  if (-not (Test-Path $TargetPath)) {
    Write-Log "File not found: $TargetPath" Error
    return $false
  }

  Write-Log "Target: $TargetPath ($([Math]::Round((Get-Item $TargetPath).Length / 1MB, 1)) MB)"
  $content = [System.IO.File]::ReadAllText($TargetPath)

  # Already patched?
  if ($content.Contains($githubAttachmentPrefix)) {
    Write-Log "Already patched — '$githubAttachmentPrefix' guard is present." Success
    return $true
  }

  # Find the vulnerable pattern
  $match = $imageUrlPattern.Match($content)
  if (-not $match.Success) {
    Write-Log "Vulnerable pattern not found in $TargetPath" Warning
    Write-Log "The extension may have been restructured. Manual inspection required." Warning
    return $false
  }

  $matchCount = $imageUrlPattern.Matches($content).Count
  if ($matchCount -ne 1) {
    Write-Log "Expected 1 match but found $matchCount — aborting to avoid unintended changes." Error
    return $false
  }

  Write-Log "Found vulnerable pattern at index $($match.Index): $($match.Value.Substring(0, 80))..."

  # Loop variable name varies between builds (was 't', later 'n', etc.) — reuse whatever was captured.
  $loopVar = $match.Groups[2].Value

  # Wrap the original one-liner with the guard:
  # Before: if(n.type===X.Image)return vscpp(Y,{src:n.imageUrl.url,...})
  # After:  if(n.type===X.Image){if(n.imageUrl.url.startsWith("..."))return;return vscpp(Y,{src:n.imageUrl.url,...})}
  $original = $match.Value
  $patched  = $original -replace `
    ('^(if\(' + [regex]::Escape($loopVar) + '\.type===\w+\.Raw\.ChatCompletionContentPartKind\.Image\))(return vscpp\(.+\))$'), `
    ('$1{if(' + $loopVar + '.imageUrl.url.startsWith("' + $githubAttachmentPrefix + '"))return;$2}')

  if ($patched -eq $original) {
    Write-Log "Inner replacement regex did not match — unexpected string format. Aborting." Error
    Write-Log "Original: $original" Error
    return $false
  }

  # Backup, write, verify
  $backup = "$TargetPath.bak"
  Write-Log "Creating backup: $backup"
  Copy-Item $TargetPath $backup -Force

  Write-Log "Applying patch..."
  $newContent = $content.Replace($original, $patched)
  [System.IO.File]::WriteAllText($TargetPath, $newContent, [System.Text.Encoding]::UTF8)

  $verify   = [System.IO.File]::ReadAllText($TargetPath)
  $oldGone  = ($imageUrlPattern.Matches($verify).Count -eq 0)
  $newThere = $verify.Contains($githubAttachmentPrefix)

  if ($oldGone -and $newThere) {
    Write-Log "Patch 1 applied successfully." Success
    return $true
  }
  else {
    Write-Log "Verification failed (oldGone=$oldGone, newThere=$newThere) — restoring backup." Error
    Copy-Item $backup $TargetPath -Force
    return $false
  }
}
#endregion

#region --- Patch 2: maxPersistedSessions limit in workbench.desktop.main.js ---
function Invoke-SessionLimitPatch {
  param([string]$TargetPath)

  Write-Host ""
  Write-Log "=== PATCH 2: Session history limit ($maxSessionsOld -> $maxSessionsNew) ===" Info

  if (-not (Test-Path $TargetPath)) {
    Write-Log "File not found: $TargetPath" Error
    return $false
  }

  Write-Log "Target: $TargetPath ($([Math]::Round((Get-Item $TargetPath).Length / 1MB, 1)) MB)"
  $content = [System.IO.File]::ReadAllText($TargetPath)

  # Find the pattern (works regardless of minified variable names)
  $match = $sessionLimitPattern.Match($content)
  if (-not $match.Success) {
    Write-Log "Session limit pattern not found in $TargetPath" Warning
    Write-Log "The workbench bundle may have been restructured. Manual inspection required." Warning
    return $false
  }

  # Already patched?
  $limitVarName = $match.Groups[1].Value
  $alreadyPatchedPattern = [regex]('var ' + [regex]::Escape($limitVarName) + '=' + $maxSessionsNew + ',')
  if ($alreadyPatchedPattern.IsMatch($content)) {
    Write-Log "Already patched — maxPersistedSessions is already $maxSessionsNew (var: $limitVarName)." Success
    return $true
  }

  $matchCount = $sessionLimitPattern.Matches($content).Count
  if ($matchCount -ne 1) {
    Write-Log "Expected 1 match but found $matchCount — aborting to avoid unintended changes." Error
    return $false
  }

  Write-Log "Found session limit at index $($match.Index): $($match.Value)"

  # Build replacement: change the numeric value from 50 to 500
  $original = $match.Value                                        # e.g. var fFo=50,Kut="chat.ChatSessionStore.index"
  $patched  = $original -replace ('var (\w+)=' + $maxSessionsOld + ','), ('var $1=' + $maxSessionsNew + ',')

  if ($patched -eq $original) {
    Write-Log "Inner replacement did not match — unexpected string format. Aborting." Error
    return $false
  }

  # Backup, write, verify
  $backup = "$TargetPath.bak"
  Write-Log "Creating backup: $backup"
  Copy-Item $TargetPath $backup -Force

  Write-Log "Applying patch..."
  $newContent = $content.Replace($original, $patched)
  [System.IO.File]::WriteAllText($TargetPath, $newContent, [System.Text.Encoding]::UTF8)

  $verify     = [System.IO.File]::ReadAllText($TargetPath)
  $oldGone    = (-not $sessionLimitPattern.IsMatch($verify))
  $newThere   = $alreadyPatchedPattern.IsMatch($verify)

  if ($oldGone -and $newThere) {
    Write-Log "Patch 2 applied successfully." Success
    Update-ProductJsonChecksum -PatchedFilePath $TargetPath
    return $true
  }
  else {
    Write-Log "Verification failed (oldGone=$oldGone, newThere=$newThere) — restoring backup." Error
    Copy-Item $backup $TargetPath -Force
    return $false
  }
}
#endregion

#region --- Main ---
$ok = $true

# --- Patch 1: image URL fix ---
if (-not $ExtensionJsPath) { $ExtensionJsPath = Find-ExtensionJs }
if ($ExtensionJsPath) {
  $ok = (Invoke-ImageUrlPatch -TargetPath $ExtensionJsPath) -and $ok
}
else {
  $ok = $false
}

# --- Patch 2: session limit fix ---
if (-not $WorkbenchJsPath) { $WorkbenchJsPath = Find-WorkbenchJs }
if ($WorkbenchJsPath) {
  $ok = (Invoke-SessionLimitPatch -TargetPath $WorkbenchJsPath) -and $ok
}
else {
  $ok = $false
}

# --- Summary ---
Write-Host ""
if ($ok) {
  Write-Log "All patches applied. Reload VS Code (Ctrl+Shift+P > Developer: Reload Window)." Success
  exit 0
}
else {
  Write-Log "One or more patches failed. Review the output above." Error
  exit 1
}
#endregion
