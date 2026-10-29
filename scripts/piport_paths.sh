# SPDX-License-Identifier: AGPL-3.0-or-later
# Where a pi-port instance keeps its files, sourced by piport_service.sh and macos_launchd.sh
# (set USER_HOME first). Each instance has a folder of its own:
#
#   ~/.pi-port/$PIPORT_INSTANCE/     (PIPORT_INSTANCE is "native" when not set)
#     piport.env        settings: AIPORT_* and PIPORT_* identity
#     state/            adopt state, device name, firmware pin (PIPORT_STATE_DIR overrides)
#     logs/piport.log   pi-port's log, rotated beside it
#     logs/launchd.log  launchd's own errors for this instance
#     restart           touch it to restart pi-port
#     service.pid
#
# PIPORT_ENV_FILE, PIPORT_LOG, PIPORT_RESTART_FILE and PIPORT_PIDFILE still override one path.
# Until `macos_launchd.sh install` moves them, a "native" instance with ~/.piport.env and no
# ~/.pi-port/native/piport.env keeps the old paths straight in the home folder.

INSTANCE=${PIPORT_INSTANCE:-native}
if ! [[ "$INSTANCE" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
    echo "PIPORT_INSTANCE must be lowercase letters, digits and -, not: $INSTANCE" >&2
    exit 1
fi
BASE=$USER_HOME/.pi-port/$INSTANCE

LEGACY_ENV_FILE=$USER_HOME/.piport.env
LEGACY_STATE_DIR=$USER_HOME/.piport-state
LEGACY_LOG=$USER_HOME/piport.log
LEGACY_RESTART_FILE=$USER_HOME/.piport-restart
LEGACY_PIDFILE=$USER_HOME/.piport-service.pid

if [ "$INSTANCE" = native ] && [ ! -e "$BASE/piport.env" ] && [ -e "$LEGACY_ENV_FILE" ]; then
    LEGACY=1
    ENV_FILE=${PIPORT_ENV_FILE:-$LEGACY_ENV_FILE}
    TRIGGER=${PIPORT_RESTART_FILE:-$LEGACY_RESTART_FILE}
    LOG=${PIPORT_LOG:-$LEGACY_LOG}
    PIDFILE=${PIPORT_PIDFILE:-$LEGACY_PIDFILE}
    DEFAULT_STATE_DIR=$LEGACY_STATE_DIR
else
    LEGACY=""
    ENV_FILE=${PIPORT_ENV_FILE:-$BASE/piport.env}
    TRIGGER=${PIPORT_RESTART_FILE:-$BASE/restart}
    LOG=${PIPORT_LOG:-$BASE/logs/piport.log}
    PIDFILE=${PIPORT_PIDFILE:-$BASE/service.pid}
    DEFAULT_STATE_DIR=$BASE/state
fi
