<#
  video-watch 安装器
  ---------------------------------------------------------------------------
  作用：把"看视频"能力的引擎装到本机，供 DSH 技能 `video-watch` 使用。
  装什么：复制 python 引擎脚本 → 建独立 venv → 装 faster-whisper → 下 small 模型（约 484MB）。
  装哪儿：默认 %LOCALAPPDATA%\dsh-video-watch（可用 -Root 改）。不写系统目录，不改 PATH。

  用法：
    powershell -ExecutionPolicy Bypass -File install.ps1
    powershell -ExecutionPolicy Bypass -File install.ps1 -Check          # 只检查，不改动
    powershell -ExecutionPolicy Bypass -File install.ps1 -SkipModel      # 先不下载模型（之后可补）
    powershell -ExecutionPolicy Bypass -File install.ps1 -FfmpegPath "C:\ffmpeg\bin\ffmpeg.exe"

  退出码：0 成功；1 有硬性缺失（看输出末尾"怎么修"）；2 仅模型未就绪（其它都好了）
#>
[CmdletBinding()]
param(
  [string]$Root = (Join-Path $env:LOCALAPPDATA 'dsh-video-watch'),
  [string]$Python,
  [string]$FfmpegPath,
  [switch]$SkipModel,
  [switch]$Check
)

# 注意：PowerShell 5.1 里原生命令写 stderr 会被当成终止错误（NativeCommandError），
# 所以这里用 Continue + 显式检查 $LASTEXITCODE，而不是 Stop。
$ErrorActionPreference = 'Continue'
$SkillDir = $PSScriptRoot
$ModelDir = Join-Path $Root 'models\faster-whisper-small'
$VenvPy   = Join-Path $Root '.venv\Scripts\python.exe'
$ApiPy    = Join-Path $Root 'scripts\kit_api.py'

function Say([string]$m)  { Write-Host $m }
function Ok([string]$m)   { Write-Host "  [OK]   $m" -ForegroundColor Green }
function Warn([string]$m) { Write-Host "  [WARN] $m" -ForegroundColor Yellow }
function Bad([string]$m)  { Write-Host "  [FAIL] $m" -ForegroundColor Red }

function Test-PythonExe([string]$exe) {
  if (-not $exe -or -not (Test-Path $exe)) { return $null }
  $ver = & $exe -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>&1
  if ($LASTEXITCODE -ne 0) { return $null }
  $ver = ($ver | Where-Object { $_ -match '^\d+\.\d+$' } | Select-Object -First 1)
  if (-not $ver) { return $null }
  if ([version]$ver -lt [version]'3.10') { return $null }
  return @{ Path = $exe; Version = $ver }
}

# ---------------------------------------------------------------- 1. python
function Resolve-Python {
  $cands = @()
  if ($Python) { $cands += $Python }

  # py 启动器（可能装了但没注册运行时，失败是正常的）
  $launcher = Get-Command py -ErrorAction SilentlyContinue
  if ($launcher) {
    foreach ($v in '3.12','3.11','3.10','3.13') {
      $out = & $launcher.Source -$v -c "import sys;print(sys.executable)" 2>&1
      if ($LASTEXITCODE -eq 0) {
        $exe = ($out | Where-Object { $_ -match '\.exe$' } | Select-Object -First 1)
        if ($exe) { $cands += $exe }
      }
    }
  }

  foreach ($n in 'python','python3') {
    $c = Get-Command $n -ErrorAction SilentlyContinue
    if ($c) { $cands += $c.Source }
  }

  # 常见安装位置（PATH 里没有也能找到）
  foreach ($v in '313','312','311','310') {
    $cands += (Join-Path $env:LOCALAPPDATA "Programs\Python\Python$v\python.exe")
    $cands += "C:\Python$v\python.exe"
    $cands += "C:\Program Files\Python$v\python.exe"
  }

  foreach ($c in ($cands | Select-Object -Unique)) {
    $hit = Test-PythonExe $c
    if ($hit) { return $hit }
  }
  return $null
}

# ---------------------------------------------------------------- 2. ffmpeg
function Resolve-Ffmpeg {
  if ($FfmpegPath) {
    if (Test-Path $FfmpegPath) { return $FfmpegPath }
    Bad "-FfmpegPath 指向的文件不存在：$FfmpegPath"; return $null
  }
  $local = Join-Path $Root 'tools\ffmpeg.exe'
  if (Test-Path $local) { return $local }
  $c = Get-Command ffmpeg -ErrorAction SilentlyContinue
  if ($c) { return $c.Source }
  return $null
}

Say ''
Say 'video-watch 安装器'
Say "  技能目录：$SkillDir"
Say "  安装目录：$Root"
if ($Check) { Say '  模式：只检查（-Check）' }
Say ''

# 结构自检：脚本必须在技能目录里
foreach ($rel in 'scripts\kit_api.py','scripts\transcribe.py','scripts\fetch_model.py','scripts\sitecustomize.py','shims\av\__init__.py') {
  if (-not (Test-Path (Join-Path $SkillDir $rel))) {
    Bad "安装包不完整，缺少 $rel（请整个文件夹一起拷，别只拷 install.ps1）"
    exit 1
  }
}

$py = Resolve-Python
if (-not $py) {
  Bad '没找到 Python 3.10+'
  Say  '  怎么修：装 https://www.python.org/downloads/ 并勾选 "Add python.exe to PATH"，然后重跑本脚本。'
  Say  '  或者用 -Python "C:\...\python.exe" 直接指定。'
  exit 1
}
Ok "Python $($py.Version)：$($py.Path)"

$ffm = Resolve-Ffmpeg
if (-not $ffm) {
  Warn '没找到 ffmpeg（画面抽帧必需）'
  Say  '  怎么修（任选一种）：'
  Say  '    a) winget install Gyan.FFmpeg，然后重开终端重跑本脚本'
  Say  '    b) 手动下载 https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'
  Say  '       解压后重跑，并加 -FfmpegPath <解压目录>\bin\ffmpeg.exe'
  Say  '  说明：本机不需要 ffprobe，时长由 ffmpeg 自身输出解析。'
} else {
  Ok "ffmpeg：$ffm"
}

if ($Check) {
  Say ''
  Say '—— 检查结果 ——'
  if (Test-Path $VenvPy) { Ok "venv：$VenvPy" } else { Warn "venv 还没建：$VenvPy" }
  if (Test-Path $ApiPy)  { Ok "引擎：$ApiPy" } else { Warn "引擎未复制：$ApiPy" }
  if (Test-Path (Join-Path $ModelDir 'model.bin')) {
    $mb = [math]::Round((Get-Item (Join-Path $ModelDir 'model.bin')).Length / 1MB, 1)
    Ok "模型：$mb MB"
  } else { Warn "模型未下载：$ModelDir" }
  if ($ffm) { Ok 'ffmpeg 可用' } else { Warn 'ffmpeg 缺失' }
  exit 0
}

# ---------------------------------------------------------------- 3. 写权限探针
# ffmpeg 对写入位置很敏感：在 DSH 沙箱里运行时它只能写当前工作区，
# 连 %LOCALAPPDATA% 和 %TEMP% 都被拒（python / PowerShell 反而能写）。
if ($ffm) {
  $probeDir = Join-Path $Root 'cache'
  try { New-Item -ItemType Directory -Force -Path $probeDir | Out-Null } catch {}
  $probe = Join-Path $probeDir 'ffmpeg-probe.jpg'
  & $ffm -y -v error -f lavfi -i "testsrc2=size=64x64:rate=1:duration=1" $probe 2>&1 | Out-Null
  $canWrite = ($LASTEXITCODE -eq 0 -and (Test-Path $probe))
  Remove-Item $probe -ErrorAction SilentlyContinue
  if ($canWrite) {
    Ok 'ffmpeg 可写安装目录'
  } else {
    Bad 'ffmpeg 无法写入安装目录 —— 抽帧会全部失败'
    Say  '  常见原因：在 DSH 沙箱里跑安装时，ffmpeg 只能写当前工作区。'
    Say  '  怎么修：把安装目录指到工作区内再跑，例如'
    Say  '    powershell -ExecutionPolicy Bypass -File install.ps1 -Root "<你的工作区>\video-kit"'
    exit 3
  }
}

# ---------------------------------------------------------------- 4. 拷引擎
Say ''
Say '[1/5] 复制引擎脚本'
try {
  foreach ($sub in 'scripts','shims','models','jobs','cache','tools') {
    New-Item -ItemType Directory -Force -Path (Join-Path $Root $sub) | Out-Null
  }
  Copy-Item -Force -Recurse (Join-Path $SkillDir 'scripts\*') (Join-Path $Root 'scripts')
  Copy-Item -Force -Recurse (Join-Path $SkillDir 'shims\*')   (Join-Path $Root 'shims')
} catch {
  Bad "复制失败：$($_.Exception.Message)"
  exit 1
}
Ok "已复制到 $Root"

# ---------------------------------------------------------------- 4. venv
Say ''
Say '[2/5] 创建独立 venv（不污染系统 Python）'
if (Test-Path $VenvPy) {
  Ok 'venv 已存在，跳过'
} else {
  # 先不带 pip 建，再用系统 Python 的 pip 往里装：比 ensurepip 稳（受限权限沙箱里也能过）
  & $py.Path -m venv --without-pip (Join-Path $Root '.venv')
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path $VenvPy)) { Bad 'venv 创建失败'; exit 1 }
  $env:PYTHONPATH = Join-Path $Root 'scripts'   # 让 sitecustomize 的 umask 补丁生效
  & $py.Path -m pip --python $VenvPy install --upgrade pip --quiet
  if ($LASTEXITCODE -ne 0) { Bad 'pip 引导失败（系统 Python 里没有可用的 pip？）'; exit 1 }
  Ok "venv：$VenvPy"
}

Say ''
Say '[3/5] 安装 faster-whisper（首次约 200MB 依赖，走清华镜像）'
# --no-cache-dir 很重要：pip 的 HTTP 缓存在某些 Windows 环境下会损坏，
# 表现为解析 numpy 这类大索引时 100% CPU 空转十几分钟（实测 618s 无进展）。
# 关掉缓存后同样的解析只要 4 秒；多花一次下载，换掉一个极难排查的卡死。
& $VenvPy -m pip install --quiet --disable-pip-version-check --no-cache-dir "faster-whisper==1.2.1" `
    -i https://pypi.tuna.tsinghua.edu.cn/simple `
    --extra-index-url https://pypi.org/simple
if ($LASTEXITCODE -ne 0) { Bad '依赖安装失败（网络？代理？）'; exit 1 }

$env:PYTHONPATH = (Join-Path $Root 'shims') + ';' + (Join-Path $Root 'scripts')
$importCheck = & $VenvPy -c "import faster_whisper, ctranslate2; print('ok')" 2>&1
if ($LASTEXITCODE -ne 0) {
  Warn "导入自检未通过：$($importCheck -join ' ')"
  Say  '  说明：Windows 的 Smart App Control 可能拦了 PyAV 的 DLL。引擎已内置 shims/av 兜底，'
  Say  '        只要把 shims 放进 PYTHONPATH 就能 import（安装器已这样设）。'
} else { Ok '依赖导入正常' }

# ---------------------------------------------------------------- 5. 模型
Say ''
Say '[4/5] 下载转写模型 faster-whisper-small（约 484MB，来源 hf-mirror.com）'
if ($SkipModel) {
  Warn '按 -SkipModel 跳过。之后单独补：'
  Say  ('  & "{0}" "{1}" --dest "{2}"' -f $VenvPy, (Join-Path $Root 'scripts\fetch_model.py'), $ModelDir)
} elseif ((Test-Path (Join-Path $ModelDir 'model.bin')) -and
          (Get-Item (Join-Path $ModelDir 'model.bin')).Length -eq 483546902) {
  Ok '模型已就绪，跳过下载'
} else {
  & $VenvPy (Join-Path $Root 'scripts\fetch_model.py') --dest $ModelDir
  if ($LASTEXITCODE -ne 0) { Bad '模型下载失败（网络？磁盘？）'; exit 2 }
}

# ---------------------------------------------------------------- 6. 冒烟
Say ''
Say '[5/5] 冒烟测试（合成 4 秒视频，抽 2 帧）'
if (-not $ffm) {
  Warn '没有 ffmpeg，跳过冒烟（其它已就绪；补上 ffmpeg 后重跑本脚本）'
} else {
  $env:DSH_VIDEO_KIT_FFMPEG = $ffm
  $tmp = Join-Path $Root 'cache\smoke.mp4'
  & $ffm -y -v error -f lavfi -i "testsrc2=size=320x180:rate=10:duration=4" -f lavfi -i "sine=frequency=440:duration=4" -shortest -pix_fmt yuv420p $tmp 2>&1 | Out-Null
  if ($LASTEXITCODE -ne 0) {
    Warn '合成测试视频失败，跳过冒烟'
  } else {
    $out = & $VenvPy $ApiPy frames --source $tmp --job smoke_install --max-frames 2 2>&1
    if ($LASTEXITCODE -eq 0) { Ok '抽帧正常' } else { Warn "抽帧报错：$($out -join ' ')" }
    if (Test-Path (Join-Path $ModelDir 'model.bin')) {
      $tr = & $VenvPy $ApiPy transcript --job smoke_install 2>&1
      if ($LASTEXITCODE -eq 0 -or $LASTEXITCODE -eq 3) { Ok '转写链路正常（静音视频返回 no_speech 属正常）' }
      else { Warn "转写报错：$($tr -join ' ')" }
    }
  }
}

# ---------------------------------------------------------------- 7. 收尾
$cfg = [ordered]@{
  root      = $Root
  python    = $VenvPy
  ffmpeg    = $ffm
  model_dir = $ModelDir
  installed = (Get-Date).ToString('s')
}
try { $cfg | ConvertTo-Json | Set-Content -Path (Join-Path $Root 'config.json') -Encoding UTF8 } catch {}

Say ''
Say '—— 完成 ——'
Ok "引擎：$ApiPy"
if (Test-Path (Join-Path $ModelDir 'model.bin')) { Ok "模型：$ModelDir" } else { Warn '模型未下载（转写不可用，抽帧可用）' }
if ($ffm) { Ok "ffmpeg：$ffm" } else { Warn 'ffmpeg 未就绪（抽帧不可用）' }
Say ''
Say '现在可以在 DSH 里直接说：「看一下 <视频链接>」。'
Say ('卸载：删掉目录 ' + $Root + ' 即可（不写注册表、不改 PATH、不往家目录落缓存）。')
exit 0
