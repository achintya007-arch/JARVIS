# Draws the JARVIS icon (an original design: a dark disc, an outer cyan ring
# with a gap, an inner arc and a bright core) and writes it as a multi-size
# ICO (PNG frames) to src\Jarvis.Body\jarvis.ico. The ICO is committed; run
# this again only to change the design.
#
#   powershell -ExecutionPolicy Bypass -File body\tools\make-icon.ps1
#
# ASCII only: Windows PowerShell 5.1 misreads UTF-8 without a BOM.

param(
    [string]$Out = (Join-Path $PSScriptRoot "..\src\Jarvis.Body\jarvis.ico")
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing

function New-Frame([int]$size) {
    $bmp = New-Object System.Drawing.Bitmap($size, $size, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.Clear([System.Drawing.Color]::Transparent)
    $s = [single]$size

    # Dark disc
    $disc = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(255, 14, 22, 33))
    $g.FillEllipse($disc, $s * 0.02, $s * 0.02, $s * 0.96, $s * 0.96)

    # Outer ring with a gap at the top right
    $w1 = [Math]::Max(1.5, $s * 0.085)
    $ring = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(255, 56, 200, 240), $w1)
    $ring.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
    $ring.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
    $m = $s * 0.16
    $g.DrawArc($ring, $m, $m, $s - 2 * $m, $s - 2 * $m, -30, 300)

    # Inner arc, opposite gap
    if ($size -ge 24) {
        $w2 = [Math]::Max(1.2, $s * 0.05)
        $inner = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(200, 56, 200, 240), $w2)
        $inner.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
        $inner.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
        $n = $s * 0.31
        $g.DrawArc($inner, $n, $n, $s - 2 * $n, $s - 2 * $n, 150, 240)
        $inner.Dispose()
    }

    # Bright core
    $c = $s * 0.13
    $core = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(255, 190, 245, 255))
    $g.FillEllipse($core, $s / 2 - $c, $s / 2 - $c, 2 * $c, 2 * $c)

    $disc.Dispose(); $ring.Dispose(); $core.Dispose(); $g.Dispose()
    $ms = New-Object System.IO.MemoryStream
    if ($size -ge 256) {
        # PNG frame (Vista+): small file for the big size.
        $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
    } else {
        # Classic DIB frame: every reader handles these at small sizes.
        $bw = New-Object System.IO.BinaryWriter($ms)
        $bw.Write([UInt32]40); $bw.Write([Int32]$size); $bw.Write([Int32]($size * 2))
        $bw.Write([UInt16]1); $bw.Write([UInt16]32); $bw.Write([UInt32]0)
        $bw.Write([UInt32]($size * $size * 4)); $bw.Write([Int32]0); $bw.Write([Int32]0)
        $bw.Write([UInt32]0); $bw.Write([UInt32]0)
        for ($y = $size - 1; $y -ge 0; $y--) {
            for ($x = 0; $x -lt $size; $x++) {
                $p = $bmp.GetPixel($x, $y)
                $bw.Write([byte]$p.B); $bw.Write([byte]$p.G); $bw.Write([byte]$p.R); $bw.Write([byte]$p.A)
            }
        }
        # AND mask: all zero (alpha does the work), rows padded to 4 bytes.
        $maskRow = [int]([Math]::Ceiling($size / 32.0) * 4)
        $bw.Write((New-Object byte[] ($maskRow * $size)))
        $bw.Flush()
    }
    $bmp.Dispose()
    return ,$ms.ToArray()
}

$sizes = 16, 24, 32, 48, 64, 128, 256
$frames = @()
foreach ($size in $sizes) { $frames += ,(New-Frame $size) }

$fs = [System.IO.File]::Create([System.IO.Path]::GetFullPath($Out))
$w = New-Object System.IO.BinaryWriter($fs)
$w.Write([UInt16]0); $w.Write([UInt16]1); $w.Write([UInt16]$sizes.Count)
$offset = 6 + 16 * $sizes.Count
for ($i = 0; $i -lt $sizes.Count; $i++) {
    $dim = if ($sizes[$i] -ge 256) { 0 } else { $sizes[$i] }
    $w.Write([byte]$dim); $w.Write([byte]$dim); $w.Write([byte]0); $w.Write([byte]0)
    $w.Write([UInt16]1); $w.Write([UInt16]32)
    $w.Write([UInt32]$frames[$i].Length); $w.Write([UInt32]$offset)
    $offset += $frames[$i].Length
}
foreach ($frame in $frames) { $w.Write($frame) }
$w.Close()
Write-Host "Wrote $Out ($offset bytes, sizes $($sizes -join ', '))"
