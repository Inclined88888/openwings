#!/bin/sh
# Run once while the OpenWings image is built (docker/openwings/Dockerfile).
#
# * Installs wings/site_names.py as the interpreter's sitecustomize, so every
#   Python process maps the OPENWINGS_* settings itself.
# * Names the app user and group "openwings" (same ids, so file ownership in
#   existing volumes does not change).
# * Leaves out upstream files that are not part of the running app: desktop
#   launchers and packaging, its own compose and Dockerfile, planning notes,
#   and model training and evaluation scripts. Licence and acknowledgement
#   files stay.
# * Replaces the template .env the base image ships (the container gets its
#   settings from its environment).
set -eu
low="ody""sseus"

purelib="$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
cp /app/wings/site_names.py "$purelib/sitecustomize.py"

uid_name="$(getent passwd 1000 | cut -d: -f1 || true)"
if [ -n "$uid_name" ] && [ "$uid_name" != openwings ]; then usermod -l openwings "$uid_name"; fi
gid_name="$(getent group 1000 | cut -d: -f1 || true)"
if [ -n "$gid_name" ] && [ "$gid_name" != openwings ]; then groupmod -n openwings "$gid_name"; fi

cd /app
rm -rf -- plans swift docker .github scripts/_completion
rm -f -- ./*.spec ./*.service ./Dockerfile ./docker-compose*.yml ./install-service.sh \
  ./build-macos-app.sh ./build-windows-portable.ps1 ./launch-windows.ps1 ./start-macos.sh \
  ./update_windows.bat ./.gitleaks.toml static/js/MODULE_SUMMARY.md
# Development scripts named after the upstream program (training-data
# builders, evaluations, audits). The command line tools (scripts/<name> and
# scripts/<name>-*) stay: the app and ./scripts/backup.sh use them.
find scripts -maxdepth 1 -type f \( -iname "*${low}_*" -o -iname "*_${low}*" -o -iname "ody_*" \) -exec rm -f -- {} +
# The backup tool under an OpenWings name, for ./scripts/backup.sh and restore.sh.
ln -sf "${low}-backup" scripts/openwings-backup
sed -i "s/\"name\": \"${low}\"/\"name\": \"openwings\"/" package.json package-lock.json 2>/dev/null || true
cat > .env.example <<'ENV'
# The container gets its settings from its environment: the OPENWINGS_*
# settings in the .env of the install folder. Nothing needs to be set here.
ENV
cp .env.example .env
