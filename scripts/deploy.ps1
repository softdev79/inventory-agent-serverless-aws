<#
.SYNOPSIS
  Build and deploy the serverless Inventory Agent (no SAM CLI or Docker needed).

.EXAMPLE
  .\scripts\deploy.ps1                     # deploy / update
  .\scripts\deploy.ps1 -Destroy            # delete everything this script created

  Reads from .env (never printed): API_KEY, OPENAI_PROJECT_ID, optional BUDGET_EMAIL.
  Requires: AWS CLI v2 signed in (`aws login`), Python 3.13 on PATH as `py -3.13`.
#>
param(
  [string]$Region = "us-east-2",
  [string]$AppName = "inventory-agent",
  # function-url (default): Lambda URL serves UI + API. cloudfront: CloudFront + S3 (needs a CloudFront-verified account)
  [ValidateSet("cloudfront", "function-url")][string]$EdgeMode = "function-url",
  [switch]$Destroy
)
# "Continue": Windows PowerShell 5.1 turns any native stderr line into a terminating error under "Stop".
# Every aws/pip call below checks $LASTEXITCODE instead.
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Stack = "$AppName-serverless"

function Read-DotEnv {
  $vals = @{}
  if (Test-Path .env) {
    foreach ($line in Get-Content .env) {
      if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') { $vals[$Matches[1]] = $Matches[2].Trim('"').Trim("'") }
    }
  }
  return $vals
}
function Invoke-Aws { & aws @args; if ($LASTEXITCODE -ne 0) { throw "aws $($args[0]) $($args[1]) failed" } }

$Account = (aws sts get-caller-identity --query Account --output text)
if ($LASTEXITCODE -ne 0) { throw "AWS CLI is not signed in. Run: aws login" }
$ArtifactBucket = "$AppName-artifacts-$Account-$Region"

if ($Destroy) {
  $ui = aws cloudformation describe-stacks --stack-name $Stack --region $Region --query "Stacks[0].Outputs[?OutputKey=='UiBucketName'].OutputValue" --output text 2>$null
  if ($ui -and $ui -ne "None") { Invoke-Aws s3 rm "s3://$ui" --recursive --region $Region }
  Invoke-Aws cloudformation delete-stack --stack-name $Stack --region $Region
  Write-Host "Waiting for stack deletion (CloudFront takes a few minutes)..."
  Invoke-Aws cloudformation wait stack-delete-complete --stack-name $Stack --region $Region
  aws s3 rb "s3://$ArtifactBucket" --force --region $Region 2>$null | Out-Null
  Write-Host "Deleted. (SSM parameter /$AppName/api-key kept; remove with: aws ssm delete-parameter --name /$AppName/api-key --region $Region)"
  return
}

$envs = Read-DotEnv
if (-not $envs["API_KEY"]) { throw "Set API_KEY in .env (the key users type into the UI)." }

# 1. Secret: X-API-Key -> SSM SecureString (free, standard tier)
Write-Host "[1/6] Storing API key in SSM Parameter Store"
Invoke-Aws ssm put-parameter --name "/$AppName/api-key" --type SecureString --value $envs["API_KEY"] --overwrite --region $Region | Out-Null

# 2. Build Lambda package: Linux arm64 wheels for Python 3.13, plus backend code
Write-Host "[2/6] Building Lambda package (manylinux arm64 wheels)"
if (Test-Path build) { Remove-Item -Recurse -Force build }
New-Item -ItemType Directory build | Out-Null
& py -3.13 -m pip install -q -r backend/requirements.txt --target build `
    --platform manylinux2014_aarch64 --implementation cp --python-version 3.13 --only-binary=:all: --upgrade
if ($LASTEXITCODE -ne 0) { throw "pip install for Lambda failed" }
Copy-Item backend/*.py build/
Copy-Item -Recurse backend/tools build/tools
New-Item -ItemType Directory build/static | Out-Null
Copy-Item frontend/index.html build/static/   # served by Lambda in function-url mode
Get-ChildItem build -Recurse -Directory -Include __pycache__, tests | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
# boto3/botocore come with the Lambda runtime; drop copies pulled in transitively to save space
foreach ($d in "boto3", "botocore", "s3transfer") { Get-ChildItem build -Directory -Filter "$d*" | Remove-Item -Recurse -Force }
$sizeMb = [math]::Round(((Get-ChildItem build -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host "      package size (unzipped): $sizeMb MB"

# 3. Artifact bucket for the zip (CloudFormation package uploads there)
Write-Host "[3/6] Uploading package"
aws s3api head-bucket --bucket $ArtifactBucket --region $Region 2>$null
if ($LASTEXITCODE -ne 0) {
  Invoke-Aws s3api create-bucket --bucket $ArtifactBucket --region $Region --create-bucket-configuration "LocationConstraint=$Region" | Out-Null
}
# Idempotent hardening (quoted: PowerShell 5.1 splits bare comma lists into separate args)
Invoke-Aws s3api put-public-access-block --bucket $ArtifactBucket --region $Region `
    --public-access-block-configuration "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
$lifecycle = Join-Path $env:TEMP "inventory-agent-lifecycle.json"
'{"Rules":[{"ID":"expire-artifacts","Status":"Enabled","Filter":{},"Expiration":{"Days":7}}]}' |
    Out-File -Encoding ascii $lifecycle
Invoke-Aws s3api put-bucket-lifecycle-configuration --bucket $ArtifactBucket --region $Region --lifecycle-configuration "file://$lifecycle"
Invoke-Aws cloudformation package --template-file template.yaml --s3-bucket $ArtifactBucket --s3-prefix $AppName `
    --output-template-file build-packaged.yaml --region $Region | Out-Null

# 4. Reserved concurrency only if the account limit allows it (new accounts have 10 total)
$limit = [int](aws lambda get-account-settings --region $Region --query "AccountLimit.ConcurrentExecutions" --output text)
$reserved = if ($limit -ge 110) { 5 } else { 0 }

# 5. Deploy
Write-Host "[4/6] Deploying CloudFormation stack $Stack in $Region"
$params = @("AppName=$AppName", "EdgeMode=$EdgeMode","ApiKeyParam=/$AppName/api-key", "ReservedConcurrency=$reserved",
            "BedrockProjectId=$($envs['OPENAI_PROJECT_ID'])")
if ($envs["BUDGET_EMAIL"]) { $params += "BudgetEmail=$($envs['BUDGET_EMAIL'])" }
Invoke-Aws cloudformation deploy --template-file build-packaged.yaml --stack-name $Stack --region $Region `
    --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND --no-fail-on-empty-changeset `
    --parameter-overrides @params

$outs = aws cloudformation describe-stacks --stack-name $Stack --region $Region --query "Stacks[0].Outputs" --output json | ConvertFrom-Json
function Get-Out($k) { ($outs | Where-Object OutputKey -eq $k).OutputValue }
$Url = Get-Out AppUrl

# 6. UI
if ($EdgeMode -eq "cloudfront") {
  Write-Host "[5/6] Uploading UI to S3 + invalidating CloudFront"
  Invoke-Aws s3 sync frontend "s3://$(Get-Out UiBucketName)/" --delete --region $Region --cache-control "no-cache" | Out-Null
  Invoke-Aws cloudfront create-invalidation --distribution-id (Get-Out DistributionId) --paths "/*" | Out-Null
} else {
  Write-Host "[5/6] UI is served by the Lambda itself (function-url mode)"
}

Write-Host "[6/6] Smoke test"
& py -3.13 scripts/smoke_deploy.py $Url
Write-Host "`nApp: $Url"
