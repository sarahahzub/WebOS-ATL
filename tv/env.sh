# webOS ATL: paths shared by the TV-side shell scripts (source this). Python programs use conf.py.
BASE=${WEBOSATL_BASE:-/media/developer/webosatl}
BIN=$BASE/bin
RUNTIME_IMG=$BASE/runtime.sqfs
RUNTIME=$BASE/runtime
LOGS=$BASE/logs
APPS=${WEBOSATL_APPS:-/media/developer/apps/usr/palm/applications}
PREFIX=org.webosatl.app.
MANAGER_ID=org.webosatl.manager
INITD=${WEBOSATL_INITD:-/var/lib/webosbrew/init.d}
HOOK=$INITD/60-webosatl
