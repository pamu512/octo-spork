param(
  [string]$EnvFile = "$PSScriptRoot/../../deploy/local-ai/.env.local"
)

$ErrorActionPreference = "Stop"
python -m local_ai_stack verify --env-file $EnvFile
