#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "Usage: $0 {start|stop|restart|status|logs}"
    exit 1
}

[ $# -eq 1 ] || usage

case "$1" in
    start|stop|restart|status)
        sudo systemctl "$1" wren
        ;;
    logs)
        sudo journalctl -u wren -n 50 -f
        ;;
    *)
        usage
        ;;
esac
