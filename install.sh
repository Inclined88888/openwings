#!/usr/bin/env bash
# OpenWings one-command installer.
#
#   curl -fsSL https://raw.githubusercontent.com/Inclined88888/openwings/main/install.sh | bash
#
# Setup options go after "bash -s --", for example:
#   curl -fsSL https://raw.githubusercontent.com/Inclined88888/openwings/main/install.sh | bash -s -- --with-browser --with-runner
#
# What it does, without asking anything: puts the code in ~/openwings (or
# updates it there), makes sure Docker is installed, running and has enough
# memory, then runs scripts/setup.sh, which starts OpenWings and prints how to
# sign in. Docker's own output goes to ~/openwings/install.log. The only
# prompt you may see is your computer's password, from sudo or Homebrew, when
# Docker has to be installed. Running it again updates the code and restarts;
# your data and .env are kept.
#
# Settings (environment variables):
#   OPENWINGS_DIR          where to install (default ~/openwings)
#   OPENWINGS_REF          branch or tag to install (default main once it holds OpenWings)
#   OPENWINGS_VERBOSE=1    show Docker's full output instead of one line per step
#   OPENWINGS_NO_OPEN=1    do not open the browser at the end
#   OPENWINGS_DOCKER_WAIT  seconds to wait for Docker to start (default 300)
#
# Works with the bash 3.2 that ships with macOS. The whole script is one
# function called on the last line, so a download cut short never runs half.

openwings_install() {
  set -euo pipefail

  local SLUG="Inclined88888/openwings"
  local REPO="https://github.com/${SLUG}"
  local RAW="https://raw.githubusercontent.com/${SLUG}"
  # Where OpenWings lives until it is merged into main.
  local PREVIEW_REF="claude/focused-hypatia-p8el3r"
  local DIR="${OPENWINGS_DIR:-$HOME/openwings}"
  local REF="${OPENWINGS_REF:-}"
  local WAIT="${OPENWINGS_DOCKER_WAIT:-300}"
  local OS USE_SG=0 SUDO="" arg no_start=0 COLIMA_MODE=0 COLIMA_MARK=0

  local G="" Y="" R="" N=""
  if [ -t 1 ]; then G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'; fi
  say()  { printf '%s==>%s %s\n' "$G" "$N" "$*"; }
  warn() { printf '%s!!%s  %s\n' "$Y" "$N" "$*" >&2; }
  die()  { printf '%sxx%s  %s\n' "$R" "$N" "$*" >&2; exit 1; }
  need() { command -v "$1" >/dev/null 2>&1 || die "$1 is needed and was not found. Install it, then run this again."; }

  case "$(uname -s)" in
    Darwin) OS=mac ;;
    Linux) if grep -qi microsoft /proc/version 2>/dev/null; then OS=wsl; else OS=linux; fi ;;
    *) die "This installer supports macOS, Linux and Windows through WSL2. On Windows, open a WSL2 terminal and run it there." ;;
  esac
  if [ "$(id -u)" -ne 0 ]; then SUDO="sudo"; fi
  if [ "$OS" = "mac" ]; then
    # Docker Desktop only links its docker command into the PATH after its
    # first start; OrbStack keeps its own. Find them either way.
    local extra
    for extra in /Applications/Docker.app/Contents/Resources/bin "$HOME/Applications/Docker.app/Contents/Resources/bin" "$HOME/.orbstack/bin"; do
      if [ -d "$extra" ]; then PATH="$PATH:$extra"; fi
    done
    export PATH
  fi
  need curl
  need tar
  for arg in ${1+"$@"}; do [ "$arg" = "--no-start" ] && no_start=1; done

  # 1. Which version ------------------------------------------------------
  if [ -z "$REF" ]; then
    if curl -fsIL --max-time 20 "${RAW}/main/scripts/setup.sh" >/dev/null 2>&1; then
      REF="main"
    elif curl -fsIL --max-time 20 "${RAW}/${PREVIEW_REF}/scripts/setup.sh" >/dev/null 2>&1; then
      REF="$PREVIEW_REF"
    else
      die "Cannot reach GitHub (${REPO}). Check the internet connection and try again."
    fi
  fi

  # 2. The code ----------------------------------------------------------
  # On a Mac without Apple's command line tools, /usr/bin/git is only a stub
  # that pops up an install dialog; treat that as no git.
  has_git() {
    if [ "$OS" = "mac" ] && [ "$(command -v git 2>/dev/null)" = "/usr/bin/git" ] && ! xcode-select -p >/dev/null 2>&1; then
      return 1
    fi
    git --version >/dev/null 2>&1
  }
  # Without git: download the release archive. On an update, the folders that
  # hold only shipped code are replaced so files removed upstream go away;
  # .env, config/ (CalDAV users) and backups/ are never touched.
  fetch_tarball() {
    local tmp kind d
    tmp="$(mktemp -d)"
    for kind in heads tags; do
      if curl -fsSL "https://codeload.github.com/${SLUG}/tar.gz/refs/${kind}/${REF}" | tar -xzf - -C "$tmp" --strip-components=1 2>/dev/null \
         && [ -f "$tmp/scripts/setup.sh" ]; then
        mkdir -p "$DIR"
        for d in compose docker docs overlay patches scripts tests; do
          if [ -d "$tmp/$d" ]; then rm -rf "${DIR:?}/$d"; fi
        done
        cp -R "$tmp/." "$DIR/"
        rm -rf "$tmp"
        return 0
      fi
      rm -rf "$tmp"; tmp="$(mktemp -d)"
    done
    rm -rf "$tmp"
    die "Could not download OpenWings (${REF}) from GitHub."
  }
  is_openwings_repo() {
    case "$(git -C "$DIR" remote get-url origin 2>/dev/null)" in
      *[/:]Inclined88888/openwings|*[/:]Inclined88888/openwings.git|*[/:]Inclined88888/wings|*[/:]Inclined88888/wings.git) return 0 ;;
    esac
    return 1
  }

  if [ -d "$DIR/.git" ]; then
    has_git || die "$DIR is a git checkout, but git does not work here. Install git (on macOS: xcode-select --install), then run this again."
    is_openwings_repo || die "$DIR is a git checkout of something other than OpenWings. Choose another folder: OPENWINGS_DIR=/path/to/folder"
    say "Updating OpenWings in $DIR"
    if [ -n "$(git -C "$DIR" status --porcelain --untracked-files=no)" ]; then
      die "$DIR has local changes to OpenWings' own files. Commit or undo them (git -C \"$DIR\" status), then run this again."
    fi
    if git -C "$DIR" fetch --quiet origin "+refs/heads/${REF}:refs/remotes/origin/${REF}" 2>/dev/null; then
      if git -C "$DIR" show-ref --verify --quiet "refs/heads/$REF"; then
        git -C "$DIR" checkout --quiet "$REF"
        git -C "$DIR" branch --quiet --set-upstream-to="origin/$REF" "$REF" 2>/dev/null || true
        git -C "$DIR" merge --quiet --ff-only "origin/$REF" \
          || die "Could not move $DIR to the latest $REF: it has commits or files that are not on GitHub. Check with: git -C \"$DIR\" status"
      else
        git -C "$DIR" checkout --quiet -b "$REF" --track "origin/$REF" \
          || die "Could not switch $DIR to $REF. Check with: git -C \"$DIR\" status"
      fi
    elif git -C "$DIR" fetch --quiet origin "+refs/tags/${REF}:refs/tags/${REF}" 2>/dev/null; then
      git -C "$DIR" checkout --quiet --detach "refs/tags/$REF" \
        || die "Could not switch $DIR to $REF. Check with: git -C \"$DIR\" status"
    else
      die "Could not fetch $REF from GitHub."
    fi
  elif [ -f "$DIR/scripts/setup.sh" ] && [ -f "$DIR/upstream.lock" ]; then
    say "Updating OpenWings in $DIR"
    fetch_tarball
  elif [ -e "$DIR" ] && { [ ! -d "$DIR" ] || [ -n "$(ls -A "$DIR" 2>/dev/null)" ]; }; then
    die "$DIR already exists and is not OpenWings. Choose another folder: OPENWINGS_DIR=/path/to/folder"
  elif has_git; then
    say "Downloading OpenWings to $DIR"
    git clone --quiet --branch "$REF" "${REPO}.git" "$DIR" || die "Could not download OpenWings from GitHub."
  else
    say "Downloading OpenWings to $DIR"
    fetch_tarball
  fi

  # 3. Docker ------------------------------------------------------------
  local LOGF="$DIR/install.log" min_gb
  ( umask 077; : >> "$LOGF" )
  printf '\n==== %s install\n' "$(date)" >> "$LOGF"
  min_gb="$( { sed -n 's/^WINGS_MIN_MEM_GB=//p' "$DIR/scripts/lib.sh" 2>/dev/null || true; } | head -n 1)"; min_gb="${min_gb:-4}"
  # run_for SECS OUT CMD...: run CMD with its output appended to OUT and stop
  # it after SECS (macOS has no timeout command). Slow steps logged to the
  # install log print a dot every 5 seconds.
  run_for() {
    local secs="$1" out="$2" pid n=0 rc=0; shift 2
    "$@" >> "$out" 2>&1 &
    pid=$!
    while kill -0 "$pid" 2>/dev/null; do
      if [ "$n" -ge "$secs" ]; then
        # Quiet the shell's own "Terminated" notice while the job is reaped.
        exec 3>&2 2>/dev/null
        kill "$pid" || true; sleep 1; kill -9 "$pid" || true
        wait "$pid" || true
        exec 2>&3 3>&-
        if [ "$out" = "$LOGF" ] && [ "$n" -ge 5 ]; then printf '\n'; fi
        return 124
      fi
      sleep 1; n=$((n + 1))
      if [ "$out" = "$LOGF" ] && [ $((n % 5)) -eq 0 ]; then printf '.'; fi
    done
    wait "$pid" || rc=$?
    if [ "$out" = "$LOGF" ] && [ "$n" -ge 5 ]; then printf '\n'; fi
    return "$rc"
  }
  docker_ok() { run_for 20 /dev/null docker info; }
  # On Linux the daemon can be running while this user may not use it yet.
  # (The output is matched, not piped: under pipefail docker's own failure
  # would hide the match.)
  docker_needs_group() {
    local out
    [ "$OS" = "mac" ] && return 1
    out="$(docker info 2>&1 || true)"
    case "$out" in *[Pp]ermission\ denied*) return 0 ;; esac
    return 1
  }
  wsl_desktop_exe() {
    local exe="/mnt/c/Program Files/Docker/Docker/Docker Desktop.exe"
    if [ -f "$exe" ]; then printf '%s' "$exe"; fi
  }

  install_docker() {
    local id=""
    case "$OS" in
      mac)
        # Colima: Docker without a desktop app, nothing to click through.
        if command -v brew >/dev/null 2>&1; then
          say "Installing Docker (Colima) with Homebrew. This takes a few minutes"
          brew install colima docker docker-compose >> "$LOGF" 2>&1 || die "Homebrew could not install Docker. Details: $LOGF"
          mkdir -p "$HOME/.docker/cli-plugins"
          ln -sfn "$(brew --prefix)/opt/docker-compose/bin/docker-compose" "$HOME/.docker/cli-plugins/docker-compose"
        else
          open "https://www.docker.com/products/docker-desktop/" 2>/dev/null || true
          die "Docker is not installed. Install Docker Desktop (the page is open), start it once, then run this command again."
        fi ;;
      linux)
        if [ -n "$SUDO" ] && ! command -v sudo >/dev/null 2>&1; then
          die "Docker is not installed and sudo is missing. Install Docker Engine as root, then run this again."
        fi
        id="$( { sed -n 's/^ID=//p' /etc/os-release 2>/dev/null || true; } | tr -d '"' | head -n 1)"
        case "$id" in
          ubuntu|debian|raspbian|fedora|centos|rhel) ;;
          *) die "Docker is not installed. Install Docker Engine and its Compose plugin for your system (https://docs.docker.com/engine/install/), then run this again." ;;
        esac
        say "Installing Docker with Docker's official script (get.docker.com). This takes a few minutes"
        curl -fsSL https://get.docker.com | $SUDO sh >> "$LOGF" 2>&1 || die "Docker's install script failed. Details: $LOGF"
        if [ -d /run/systemd/system ]; then $SUDO systemctl enable --now docker >> "$LOGF" 2>&1 || true; fi ;;
      wsl)
        die "Docker is not available in this WSL terminal. Install Docker Desktop for Windows, turn on Settings > Resources > WSL integration for this distribution, then run this again." ;;
    esac
  }

  start_docker() {
    case "$OS" in
      mac)
        if [ -d /Applications/Docker.app ] || [ -d "$HOME/Applications/Docker.app" ]; then
          say "Starting Docker Desktop"; open -a Docker
        elif [ -d /Applications/OrbStack.app ] || [ -d "$HOME/Applications/OrbStack.app" ]; then
          say "Starting OrbStack"; open -a OrbStack
        else
          die "Docker is installed but no Docker app was found to start. Start your Docker engine, then run this again."
        fi ;;
      linux)
        say "Starting Docker"
        if command -v systemctl >/dev/null 2>&1 && systemctl --user cat docker-desktop.service >/dev/null 2>&1; then
          systemctl --user start docker-desktop || true
        elif command -v systemctl >/dev/null 2>&1 && systemctl --user cat docker.service >/dev/null 2>&1; then
          systemctl --user start docker || true        # rootless Docker
        elif command -v snap >/dev/null 2>&1 && snap list docker >/dev/null 2>&1; then
          $SUDO snap start docker || true
        elif [ -d /run/systemd/system ]; then
          $SUDO systemctl start docker || true
        elif command -v service >/dev/null 2>&1; then
          $SUDO service docker start || true
        fi ;;
      wsl)
        local exe; exe="$(wsl_desktop_exe)"
        if [ -n "$exe" ]; then
          say "Starting Docker Desktop for Windows"; "$exe" >/dev/null 2>&1 &
        else
          die "Start Docker Desktop for Windows (with WSL integration on for this distribution), then run this again."
        fi ;;
    esac
  }

  wait_docker() {
    local limit="${1:-$WAIT}" waited=0
    printf 'Waiting for Docker'
    while ! docker_ok && ! docker_needs_group; do
      if [ "$waited" -ge "$limit" ]; then printf '\n'; return 1; fi
      if [ "$waited" -eq 30 ] && [ "${COLIMA_MODE:-0}" -eq 0 ]; then
        printf '\n'; warn "Docker is still starting. The first start can take a minute or two; if Docker shows a window, accept it."
        printf 'Waiting for Docker'
      fi
      printf '.'; sleep 2; waited=$((waited + 2))
    done
    printf ' ready\n'
  }

  # --- Colima (macOS) ---------------------------------------------------
  # Colima answers "start" with success whenever its machine is up, even when
  # Docker inside it is down (an interrupted stop leaves exactly that), and it
  # saves new settings such as memory to colima.yaml without applying them.
  # So Docker is reached through Colima's own socket and judged only by
  # whether it answers, memory is read from Docker, and every colima command
  # has a time limit (it can hang on a stuck machine). Nothing here deletes
  # Colima's machine, images or volumes.
  colima_base() {
    if [ -n "${COLIMA_HOME:-}" ] && [ -d "$COLIMA_HOME" ]; then printf '%s' "$COLIMA_HOME"
    elif [ -d "$HOME/.colima" ]; then printf '%s' "$HOME/.colima"
    elif [ -n "${XDG_CONFIG_HOME:-}" ]; then printf '%s' "$XDG_CONFIG_HOME/colima"
    elif [ -d "$HOME/.config/colima" ]; then printf '%s' "$HOME/.config/colima"
    else printf '%s' "$HOME/.colima"; fi
  }
  colima_use_socket() { DOCKER_HOST="unix://$(colima_base)/default/docker.sock"; export DOCKER_HOST; }
  colima_state() {
    local line
    line="$( { colima list --json 2>/dev/null | grep -E '"name": ?"default"' || true; } | head -n 1)"
    case "$line" in
      '') echo Missing ;;
      *'"Running"'*) echo Running ;;
      *'"Stopped"'*) echo Stopped ;;
      *) echo Other ;;
    esac
  }
  docker_mem_gb() {
    local bytes
    bytes="$(run_for 20 /dev/stdout docker info --format '{{.MemTotal}}' 2>/dev/null || true)"
    case "$bytes" in ''|*[!0-9]*) echo 0 ;; *) echo $(( (bytes + 536870912) / 1073741824 )) ;; esac
  }
  # Stop whatever state Colima is in (gently, then by force), then start it.
  colima_fresh_start() {
    case "$(colima_state)" in
      Missing|Stopped) ;;
      Running) run_for "${OPENWINGS_COLIMA_STOP_LIMIT:-120}" "$LOGF" colima stop || run_for 60 "$LOGF" colima stop --force || true ;;
      *) run_for 60 "$LOGF" colima stop --force || true ;;
    esac
    run_for 300 "$LOGF" colima start --memory "$1" || true
    colima_use_socket
  }
  colima_give_up() {
    warn "Docker (Colima) did not come up. What Colima said:"
    { tail -n +"$((COLIMA_MARK + 1))" "$LOGF" | grep -iE 'error|fatal|warn' || true; } | tail -n 8 >&2
    die "Restart your Mac, then run this command again. If it still fails, run: colima delete   then this command again (that erases Colima's virtual machine and may remove downloaded Docker images; OpenWings downloads them again)."
  }
  colima_ensure() {
    local cfg start_mem have
    COLIMA_MODE=1
    COLIMA_MARK=$(wc -l < "$LOGF")
    cfg="$( { sed -n 's/^memory: *\([0-9][0-9]*\).*/\1/p' "$(colima_base)/default/colima.yaml" 2>/dev/null || true; } | head -n 1)"
    start_mem="$min_gb"
    if [ -n "$cfg" ] && [ "$cfg" -gt "$min_gb" ]; then start_mem="$cfg"; fi
    colima_use_socket
    if ! docker_ok; then
      case "$(colima_state)" in
        Missing|Stopped)
          say "Starting Docker (Colima) with ${start_mem} GB of memory"
          run_for 300 "$LOGF" colima start --memory "$start_mem" || true
          colima_use_socket ;;
        *)
          say "Docker (Colima) is not answering; restarting Colima (your images and data are kept)"
          colima_fresh_start "$start_mem" ;;
      esac
      if ! wait_docker "${OPENWINGS_COLIMA_WAIT:-120}"; then
        say "Still not answering; restarting Colima by force once"
        run_for 60 "$LOGF" colima stop --force || true
        run_for 300 "$LOGF" colima start --memory "$start_mem" || true
        colima_use_socket
        wait_docker "${OPENWINGS_COLIMA_WAIT:-120}" || colima_give_up
      fi
    fi
    have="$(docker_mem_gb)"
    if [ "$have" -gt 0 ] && [ "$have" -lt "$min_gb" ]; then
      say "Giving Docker (Colima) ${min_gb} GB of memory instead of ${have} GB; Colima restarts once"
      colima_fresh_start "$min_gb"
      wait_docker 180 || colima_give_up
    fi
    # Point the docker command at Colima for later use too, as a normal
    # Colima start does (an interrupted stop removes this).
    if ! docker context inspect colima >/dev/null 2>&1; then
      docker context create colima --description colima --docker "host=$DOCKER_HOST" >> "$LOGF" 2>&1 || true
    fi
    docker context use colima >> "$LOGF" 2>&1 || true
  }
  other_mac_engine() {
    [ -d /Applications/Docker.app ] || [ -d "$HOME/Applications/Docker.app" ] \
      || [ -d /Applications/OrbStack.app ] || [ -d "$HOME/Applications/OrbStack.app" ]
  }

  if [ "$no_start" -eq 0 ]; then
    if ! command -v docker >/dev/null 2>&1; then
      # Docker Desktop for Windows adds docker to WSL only while it runs.
      if [ "$OS" = "wsl" ] && [ -n "$(wsl_desktop_exe)" ]; then
        start_docker
        wait_docker || die "Docker Desktop did not make docker available in WSL within ${WAIT} seconds. In Docker Desktop, turn on Settings > Resources > WSL integration for this distribution, then run this again."
      else
        install_docker
      fi
    fi
    command -v docker >/dev/null 2>&1 || die "Docker was not found after installing. Open a new terminal and run this again."
    if [ "$OS" = "mac" ] && command -v colima >/dev/null 2>&1 \
       && ! { docker_ok && [ "$(docker context show 2>/dev/null)" != "colima" ]; } \
       && { [ -d "$(colima_base)/default" ] || ! other_mac_engine; }; then
      colima_ensure
    elif ! docker_ok && ! docker_needs_group; then
      start_docker
      wait_docker || die "Docker did not start within ${WAIT} seconds. Start it yourself, then run this command again."
    fi
    if ! docker_ok && docker_needs_group; then
      local me; me="$(id -un)"
      say "Letting your user ($me) use Docker (adds it to the docker group)"
      $SUDO usermod -aG docker "$me" || die "Could not add $me to the docker group."
      command -v sg >/dev/null 2>&1 || die "Added $me to the docker group. Log out and back in, then run this again."
      USE_SG=1
    fi
    if [ "$USE_SG" -eq 1 ]; then
      sg docker -c "docker compose version" >/dev/null 2>&1 || die "Docker Compose v2 is missing. Install Docker's Compose plugin (https://docs.docker.com/compose/install/linux/), then run this again."
    else
      docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is missing. Install Docker's Compose plugin (https://docs.docker.com/compose/install/), then run this again."
    fi
  fi

  # 4. Setup -------------------------------------------------------------
  cd "$DIR"
  chmod +x scripts/*.sh 2>/dev/null || true
  if [ "$USE_SG" -eq 1 ]; then
    # sg hands one command line to /bin/sh: quote each option for sh.
    local cmd="./scripts/setup.sh" q
    [ "${OPENWINGS_VERBOSE:-}" = "1" ] || cmd="$cmd --quiet"
    for arg in ${1+"$@"}; do
      q="$(printf '%s' "$arg" | sed "s/'/'\\\\''/g")"
      cmd="$cmd '$q'"
    done
    sg docker -c "$cmd" < /dev/null
  else
    if [ "${OPENWINGS_VERBOSE:-}" = "1" ]; then
      ./scripts/setup.sh ${1+"$@"} < /dev/null
    else
      ./scripts/setup.sh --quiet ${1+"$@"} < /dev/null
    fi
  fi

  # 5. Open it -----------------------------------------------------------
  local url="" prev="" bind port
  for arg in ${1+"$@"}; do
    [ "$prev" = "--domain" ] && url="https://$arg"
    prev="$arg"
  done
  if [ -z "$url" ]; then
    bind="$( { sed -n 's/^APP_BIND=//p' .env 2>/dev/null || true; } | tail -n 1)"; bind="${bind:-127.0.0.1}"
    port="$( { sed -n 's/^APP_PORT=//p' .env 2>/dev/null || true; } | tail -n 1)"; port="${port:-7011}"
    [ "$bind" = "0.0.0.0" ] && bind="localhost"
    url="http://${bind}:${port}"
  fi
  if [ "$no_start" -eq 0 ] && [ "${OPENWINGS_NO_OPEN:-}" != "1" ]; then
    case "$OS" in
      mac) open "$url" 2>/dev/null || true ;;
      wsl) explorer.exe "$url" >/dev/null 2>&1 || true ;;
      linux) if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && command -v xdg-open >/dev/null 2>&1; then xdg-open "$url" >/dev/null 2>&1 || true; fi ;;
    esac
  fi
  if [ "$USE_SG" -eq 1 ]; then
    say "Installed in $DIR. Log out and back in once so this user can use Docker directly."
  else
    say "Installed in $DIR. Stop: cd \"$DIR\" && docker compose stop   Start again: cd \"$DIR\" && docker compose up -d"
  fi
}

# Same line as the call: a copy of this file replaced while it runs (an
# update over itself) is never read further.
openwings_install "$@"; exit $?
