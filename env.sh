# Sourced by the *.sh scripts. Loads .env for any ATAI_* variable the shell
# hasn't set (the shell wins, as in atai_api.py) and builds BASE_URL={root}/api.
_env="$(dirname "${BASH_SOURCE[0]}")/.env"
if [ -f "$_env" ]; then
  while IFS="=" read -r k v || [ -n "$k" ]; do
    case "$k" in ''|\#*) continue ;; esac
    v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
    if [ -n "${!k:-}" ] && [ "${!k}" != "$v" ]; then
      echo "warning: $k from your shell overrides .env (unset it to use .env)" >&2
    else
      export "$k=$v"
    fi
  done < "$_env"
fi
: "${ATAI_API_KEY:?Set ATAI_API_KEY (see .env.example)}"
: "${ATAI_API_ENDPOINT:?Set ATAI_API_ENDPOINT (see .env.example)}"
BASE_URL="${ATAI_API_ENDPOINT%/}"
BASE_URL="${BASE_URL%/v0.5}"
BASE_URL="${BASE_URL%/api}/api"
