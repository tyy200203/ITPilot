param(
    [switch]$NoCache
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$buildArgs = @("compose", "build")
if ($NoCache) {
    $buildArgs += "--no-cache"
}
$buildArgs += "itpilot"

Write-Host "[1/4] Building the ITPilot image..." -ForegroundColor Cyan
& docker @buildArgs
if ($LASTEXITCODE -ne 0) {
    throw "Docker image build failed."
}

Write-Host "[2/4] Recreating the application container..." -ForegroundColor Cyan
& docker compose up -d --force-recreate itpilot
if ($LASTEXITCODE -ne 0) {
    throw "ITPilot container recreation failed."
}

Write-Host "[3/4] Waiting for the API..." -ForegroundColor Cyan
$health = $null
for ($attempt = 1; $attempt -le 60; $attempt++) {
    try {
        $health = Invoke-RestMethod -Uri "http://localhost:8000/health" -TimeoutSec 5
        if ($health.status -eq "ok") {
            break
        }
    }
    catch {
        Start-Sleep -Seconds 2
    }
}

if ($null -eq $health -or $health.status -ne "ok") {
    throw "ITPilot API did not become ready. Run: docker compose logs itpilot"
}

Write-Host "[4/4] Verifying the running code and knowledge base..." -ForegroundColor Cyan
$agentNames = @($health.agents.PSObject.Properties.Name)
if ($health.service -ne "ITPilot") {
    throw "The running service is not ITPilot: service=$($health.service)"
}
if ($health.build_id -ne "phase9-final-deployment-v1") {
    throw "Unexpected build ID: build_id=$($health.build_id)"
}
if ($agentNames -contains "billing_0") {
    throw "The legacy billing_0 agent is still present."
}
foreach ($requiredAgent in @("general_0", "technical_0", "permission_0", "escalation_0")) {
    if ($agentNames -notcontains $requiredAgent) {
        throw "Missing required agent: $requiredAgent"
    }
}
if ($health.knowledge.collection_name -ne "itpilot_knowledge_base_v1") {
    throw "Unexpected knowledge collection: $($health.knowledge.collection_name)"
}

$stats = Invoke-RestMethod -Uri "http://localhost:8000/knowledge/stats" -TimeoutSec 10
if ($stats.corpus_id -ne "itpilot-it-service-v1") {
    throw "Unexpected knowledge corpus: $($stats.corpus_id)"
}
if (@($stats.document_titles).Count -lt 8) {
    throw "The IT knowledge base contains fewer than 8 documents."
}

Write-Host "ITPilot rebuild and verification passed." -ForegroundColor Green
Write-Host "service: $($health.service)"
Write-Host "version: $($health.version)"
Write-Host "build_id: $($health.build_id)"
Write-Host "agents: $($agentNames -join ', ')"
Write-Host "collection: $($stats.collection_name)"
Write-Host "documents: $(@($stats.document_titles).Count)"
