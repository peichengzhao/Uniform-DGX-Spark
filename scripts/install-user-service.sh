#!/usr/bin/env bash

set -euo pipefail

project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
service_dir="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
service_file="${service_dir}/uniform-dgxspark.service"

mkdir -p "${service_dir}"
sed "s|@PROJECT_DIR@|${project_dir}|g" \
  "${project_dir}/packaging/uniform-dgxspark.service.in" >"${service_file}"
systemctl --user daemon-reload
systemctl --user enable --now uniform-dgxspark.service

printf 'Installed %s\n' "${service_file}"
printf 'Open http://127.0.0.1:18180\n'
