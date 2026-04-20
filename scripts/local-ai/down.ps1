param(
  [string]$EnvFile = "$PSScriptRoot/../../deploy/local-ai/.env.local"
)

$ErrorActionPreference = "Stop"
python -m local_ai_stack down --env-file $EnvFile
