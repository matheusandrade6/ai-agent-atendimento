<#
Equivalente do Makefile para Windows, onde `make` normalmente nao existe.

Uso:
    .\tasks.ps1 up        # sobe Postgres + Redis
    .\tasks.ps1 down
    .\tasks.ps1 install   # cria o venv e instala as dependencias
    .\tasks.ps1 migrate
    .\tasks.ps1 test
    .\tasks.ps1 lint
    .\tasks.ps1 check     # migrate + test + lint, para fechar uma sessao
    .\tasks.ps1 dev

Se o PowerShell recusar a execucao do script:
    Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#>

param(
    [Parameter(Position = 0)]
    [ValidateSet("up", "down", "install", "migrate", "test", "lint", "fmt", "check", "dev", "worker")]
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
        docker compose up -d postgres redis
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
        & $Py -m pip install --upgrade pip
        & $Py -m pip install -e ".[dev]"
    }

    "migrate" {
        Assert-Venv
        & $Py -m alembic upgrade head
    }

    "test" {
        Assert-Venv
        & $Py -m pytest
    }

    "lint" {
        Assert-Venv
        & $Py -m ruff check app tests migrations
        & $Py -m ruff format --check app tests migrations
        & $Py -m mypy app
    }

    "fmt" {
        Assert-Venv
        & $Py -m ruff check --fix app tests migrations
        & $Py -m ruff format app tests migrations
    }

    "check" {
        Assert-Venv
        Write-Host "== migrations ==" -ForegroundColor Cyan
        & $Py -m alembic upgrade head
        Write-Host "`n== testes ==" -ForegroundColor Cyan
        & $Py -m pytest
        Write-Host "`n== lint e tipos ==" -ForegroundColor Cyan
        & $Py -m ruff check app tests migrations
        & $Py -m ruff format --check app tests migrations
        & $Py -m mypy app
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
