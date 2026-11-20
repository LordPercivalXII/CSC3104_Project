param([switch]$SkipBuild)

# Launch the existing kind architecture with mock inference from the first pod.
# No Gemini Secret or Docker Desktop Kubernetes feature is required for this demo.
$ErrorActionPreference = 'Stop'
$demoRepoRoot = Split-Path -Parent $PSScriptRoot
$demoCluster = 'llm-gateway'
$demoContext = 'kind-llm-gateway'

function Invoke-DemoCommand {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Executable failed with exit code $LASTEXITCODE" }
}

function Apply-DemoKustomization {
    param([string]$Directory)
    $rendered = & kubectl kustomize --load-restrictor LoadRestrictionsNone $Directory
    if ($LASTEXITCODE -ne 0) { throw "Could not render $Directory" }
    $rendered | & kubectl --context $demoContext apply -f -
    if ($LASTEXITCODE -ne 0) { throw "Could not apply $Directory" }
}

foreach ($tool in @('docker', 'kind', 'kubectl')) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "$tool is missing. See demo/README.md for prerequisites."
    }
}

Push-Location $demoRepoRoot
try {
    Invoke-DemoCommand -Executable docker -Arguments @('info', '--format', '{{.OSType}}')
    $clusters = & kind get clusters
    if ($LASTEXITCODE -ne 0) { throw 'Could not list kind clusters. Is Docker Desktop running?' }
    if ($demoCluster -notin $clusters) {
        Invoke-DemoCommand -Executable kind -Arguments @('create', 'cluster', '--config', 'k8s/kind-config.yaml')
    }
    if (-not $SkipBuild) {
        Invoke-DemoCommand -Executable docker -Arguments @('build', '-t', 'smart-gateway:dev', '.')
    }
    Invoke-DemoCommand -Executable kind -Arguments @('load', 'docker-image', 'smart-gateway:dev', '--name', $demoCluster)

    Invoke-DemoCommand -Executable kubectl -Arguments @('--context', $demoContext, 'apply', '-f',
        'https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml')
    $metricsPatch = '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
    # --patch-file avoids PowerShell/native JSON argument quoting differences.
    $patchFile = Join-Path ([IO.Path]::GetTempPath()) ([IO.Path]::GetRandomFileName())
    try {
        [IO.File]::WriteAllText($patchFile, $metricsPatch)
        Invoke-DemoCommand -Executable kubectl -Arguments @('--context', $demoContext, '-n', 'kube-system', 'patch',
            'deployment', 'metrics-server', '--type=json', '--patch-file', $patchFile)
    } finally {
        Remove-Item -LiteralPath $patchFile -ErrorAction SilentlyContinue
    }

    Apply-DemoKustomization 'demo/k8s/gateway-mock'
    Invoke-DemoCommand -Executable kubectl -Arguments @('--context', $demoContext, '-n', 'llm-gateway', 'rollout',
        'restart', 'deployment/gateway')
    foreach ($resource in @('statefulset/qdrant', 'statefulset/redis', 'deployment/gateway')) {
        Invoke-DemoCommand -Executable kubectl -Arguments @('--context', $demoContext, '-n', 'llm-gateway', 'rollout',
            'status', $resource, '--timeout=600s')
    }
    Apply-DemoKustomization 'demo/k8s'
    foreach ($resource in @('deployment/demo-prometheus', 'deployment/demo-grafana', 'deployment/demo-locust')) {
        Invoke-DemoCommand -Executable kubectl -Arguments @('--context', $demoContext, '-n', 'llm-gateway', 'rollout',
            'status', $resource, '--timeout=300s')
    }
    Write-Host 'Mock Kubernetes demo ready: http://localhost:8080'
    Write-Host 'Start the three port-forwards in demo/README.md, then open Locust and Grafana.'
    Write-Host 'No load test starts automatically. This mock overlay suspends the normal cache-sweep CronJob.'
} finally {
    Pop-Location
}
