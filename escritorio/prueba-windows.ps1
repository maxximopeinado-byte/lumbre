# Instala Lumbre en Windows real y traduce un PDF con el motor de prueba.
$ErrorActionPreference = 'Stop'
$installer = (Get-ChildItem entrega/Lumbre-instalador-*.exe).FullName
Start-Process $installer -ArgumentList '/S' -Wait
$app = Join-Path $env:LOCALAPPDATA 'Programs\Lumbre\Lumbre.exe'
if (-not (Test-Path $app)) { throw 'No se ha instalado Lumbre.exe' }
Start-Process $app
$url = 'http://127.0.0.1:8765'
$ready = $false
foreach ($i in 1..40) {
  Start-Sleep 3
  try { Invoke-RestMethod "$url/api/estado" | Out-Null; $ready = $true; break } catch {}
}
if (-not $ready) { Get-Content (Join-Path $env:LOCALAPPDATA 'Lumbre\lumbre.log'); throw 'El servidor no arranca' }
$python = Join-Path $env:LOCALAPPDATA 'Programs\Lumbre\runtime\python.exe'
$env:PATH = "$(Split-Path $python);$(Split-Path $python)\Library\bin;$env:PATH"
& $python -c "import pymupdf;d=pymupdf.open();[d.new_page().insert_text((50,80),'The social contract is not a historical event but a way of asking what authority could be justified.') for _ in range(3)];d.save('prueba.pdf')"
$upload = curl.exe -s -F "file=@prueba.pdf" "$url/api/libros" | ConvertFrom-Json
Invoke-RestMethod -Method Post -ContentType 'application/json' -Body '{"motor":"prueba","origen":"en","destino":"es"}' "$url/api/libros/$($upload.id)/traducir" | Out-Null
foreach ($i in 1..60) {
  Start-Sleep 2
  $job = Invoke-RestMethod "$url/api/libros/$($upload.id)"
  if ($job.estado -notin @('traduciendo','en cola')) { break }
}
if ($job.estado -ne 'terminado') { throw "Estado: $($job.estado) $($job.mensaje)" }
Invoke-WebRequest "$url/api/libros/$($upload.id)/pagina/traducido/1.jpg" -OutFile pagina.jpg
if ((Get-Item pagina.jpg).Length -lt 2000) { throw 'El visor no devuelve la página' }
Write-Output "Correcto: $($job.estado)"
