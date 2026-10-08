#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

readonly -a SERVICE_ACTIONS=(start stop restart run status)
readonly -a RELEASE_ACTIONS=(install-dev install-prod publish upgrade rollback uninstall)
readonly -a ACTIONS=("${SERVICE_ACTIONS[@]}" "${RELEASE_ACTIONS[@]}")

usage() {
  printf 'Usage: %s <start|stop|restart|run|status|install-dev|install-prod|publish|upgrade> [version]\n' "${0##*/}" >&2
  printf '       %s rollback <version> | uninstall\n' "${0##*/}" >&2
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 2
}

install_prod() {
  local version="${1:-}"
  if [[ -z "${version}" ]] && uv tool list | grep -q '^funmill-api '; then
    printf 'funmill-api is already installed; no version requested, skipping upgrade.\n'
  elif [[ -n "${version}" ]]; then
    uv tool install "funmill-api==${version}"
  else
    uv tool install funmill-api
  fi
}

contains() {
  local needle="$1"
  shift
  local item
  for item in "$@"; do
    [[ "${item}" == "${needle}" ]] && return 0
  done
  return 1
}

main() {
  local action="${1:-}"
  shift || true

  [[ -n "${action}" ]] || {
    usage
    die "missing action"
  }
  contains "${action}" "${ACTIONS[@]}" || {
    usage
    die "unknown action: ${action}"
  }

  case "${action}" in
    start)
      (( $# == 0 )) || die "start takes no arguments"
      funmill server start
      ;;
    run)
      (( $# == 0 )) || die "run takes no arguments"
      exec funmill server run
      ;;
    stop)
      (( $# == 0 )) || die "stop takes no arguments"
      funmill server stop
      ;;
    status)
      (( $# == 0 )) || die "status takes no arguments"
      funmill server status
      ;;
    restart)
      (( $# == 0 )) || die "restart takes no arguments"
      funmill server restart
      ;;
    install-dev)
      (( $# == 0 )) || die "install-dev takes no arguments"
      uv sync
      ;;
    install-prod)
      (( $# <= 1 )) || die "install-prod accepts at most one version"
      install_prod "${1:-}"
      ;;
    publish)
      (( $# == 0 )) || die "publish takes no arguments"
      uv build
      ;;
    upgrade)
      (( $# <= 1 )) || die "upgrade accepts at most one version"
      funmill upgrade "$@"
      ;;
    rollback)
      (( $# == 1 )) || die "rollback requires an explicit version: ${0##*/} rollback <version>"
      funmill rollback "$1"
      ;;
    uninstall)
      (( $# == 0 )) || die "uninstall takes no arguments"
      funmill uninstall
      ;;
  esac
}

main "$@"
