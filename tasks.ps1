<#
Equivalente do Makefile para Windows, onde `make` normalmente nao existe.

Uso:
    .\tasks.ps1 up        # sobe Postgres + Redis
    .\tasks.ps1 down
    .\tasks.ps1 install   # cria o venv e instala as dependencias
    .\tasks.ps1 migrate
    .\tasks.ps1 test
    .\tasks.ps1 conv      # so a suite conversacional (19.4)
    .\tasks.ps1 lint
    .\tasks.ps1 check     # migrate + test + lint, para fechar uma sessao
    .\tasks.ps1 dev

Se o PowerShell recusar a execucao do script:
    Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#>

param(
    [Parameter(Position = 0)]
    [ValidateSet("up", "down", "install", "migrate", "test", "conv", "lint", "fmt", "check", "dev", "worker")]
    [string]$Task = "check"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$Py = ".\.venv\Scripts\python.exe"

function Assert-Venv {
    if (-not (Test-Path $Py)) {
        throw "venv nao encontrado. Rode primeiro: .\tasks.ps1 install"
    }
}

# O PowerShell 5.1 nao interrompe o script quando um comando nativo sai com codigo != 0:
# `$ErrorActionPreference = "Stop"` so cobre erros do proprio PowerShell. Sem esta checagem,
# `check` imprimia "Tudo verde." com teste falhando. Cada comando nativo que decide o
# resultado passa por aqui, e o script para no primeiro que falhar.
function Invoke-Step {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][scriptblock]$Command
    )
    $global:LASTEXITCODE = 0
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Name falhou (codigo de saida $LASTEXITCODE)."
    }
}

function Assert-Docker {
    docker info *> $null
    if (-not $?) {
        throw @"
O engine do Docker nao esta respondendo.

Abra o Docker Desktop e espere o icone da baleia ficar estavel, depois repita.
Se o servico estiver parado, um PowerShell como Administrador resolve:
    Start-Service com.docker.service
"@
    }
}

switch ($Task) {
    "up" {
        Assert-Docker
        Invoke-Step "docker compose up" { docker compose up -d postgres redis }
        Write-Host "`nPostgres em localhost:5433, Redis em localhost:6380" -ForegroundColor Green
    }

    "down" { docker compose down }

    "install" {
        if (-not (Test-Path ".\.venv")) {
            # O projeto tem como alvo o 3.12 (Docker e CI). Localmente, 3.12 ou 3.13 servem.
            $exe = (Get-Command py -ErrorAction SilentlyContinue)
            if ($exe) { py -3.12 -m venv .venv 2>$null; if (-not $?) { py -3.13 -m venv .venv } }
            else { python -m venv .venv }
        }
        Invoke-Step "pip upgrade" { & $Py -m pip install --upgrade pip }
        Invoke-Step "pip install" { & $Py -m pip install -e ".[dev]" }
    }

    "migrate" {
        Assert-Venv
        Invoke-Step "alembic" { & $Py -m alembic upgrade head }
    }

    "test" {
        Assert-Venv
        Invoke-Step "pytest" { & $Py -m pytest }
    }

    "conv" {
        # A 19.4 manda rodar a suite inteira a cada mudanca de prompt. Ela nao precisa
        # de docker nem de chave de API.
        Assert-Venv
        Invoke-Step "pytest conversacional" { & $Py -m pytest -m conversational }
    }

    "lint" {
        Assert-Venv
        Invoke-Step "ruff check" { & $Py -m ruff check app tests migrations }
        Invoke-Step "ruff format" { & $Py -m ruff format --check app tests migrations }
        Invoke-Step "mypy" { & $Py -m mypy app }
    }

    "fmt" {
        Assert-Venv
        Invoke-Step "ruff check --fix" { & $Py -m ruff check --fix app tests migrations }
        Invoke-Step "ruff format" { & $Py -m ruff format app tests migrations }
    }

    "check" {
        Assert-Venv
        Write-Host "== migrations ==" -ForegroundColor Cyan
        Invoke-Step "alembic" { & $Py -m alembic upgrade head }
        Write-Host "`n== testes ==" -ForegroundColor Cyan
        Invoke-Step "pytest" { & $Py -m pytest }
        Write-Host "`n== lint e tipos ==" -ForegroundColor Cyan
        Invoke-Step "ruff check" { & $Py -m ruff check app tests migrations }
        Invoke-Step "ruff format" { & $Py -m ruff format --check app tests migrations }
        Invoke-Step "mypy" { & $Py -m mypy app }
        Write-Host "`nTudo verde." -ForegroundColor Green
    }

    "dev" {
        Assert-Venv
        & $Py -m uvicorn app.main:app --reload --port 8000
    }

    "worker" {
        Assert-Venv
        & $Py -m arq app.workers.settings.WorkerSettings
    }
}
