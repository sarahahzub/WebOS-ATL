#!/bin/sh
# ls2-role.sh [install|remove]: luna-service2 role for /media/developer/webosatl/bin/atlvdec (run on the TV as root).
# NDL (uMediaClient) registers com.webos.media.client.* / com.webos.rm.client.* names; ls-hubd only allows that
# for executables that have a role. Wrapper apps have one for bin/run (a shell script), so the helper gets its
# own, cloned from an installed wrapper app's role with exeName = the helper. apkweb runs this after an install.
R=/var/palm/ls2-dev/roles; M=/var/luna-service2-dev; N=org.webosatl.atlvdec; X=/media/developer/webosatl/bin/atlvdec
if [ "$1" = remove ]; then
	rm -f $R/prv/$N.json $R/pub/$N.json $M/manifests.d/$N.json $M/client-permissions.d/$N.app.json
	ls-control scan-services >/dev/null 2>&1; exit 0
fi
[ -f $M/manifests.d/$N.json ] && [ "$1" != force ] && { echo "role present"; exit 0; }
A=$(ls $M/manifests.d 2>/dev/null | sed -n 's/^\(org\.webosatl\.app\.[^/]*\)\.json$/\1/p' | head -1)
[ -n "$A" ] || { echo "no wrapper app role to clone yet (install an app first)"; exit 1; }
# build the four files in a temp dir and check them before anything goes live: a broken role file in these dirs can
# break other developer-mode services (Homebrew Channel's too)
T=$(mktemp -d 2>/dev/null) || { T=/tmp/webosatl-role.$$; mkdir -m 700 "$T" || exit 1; }
trap 'rm -rf "$T"' EXIT
for t in prv pub; do
	sed -e "s#\"exeName\":\"[^\"]*\"#\"exeName\":\"$X\"#" -e "s#\"$A\"#\"org.webosatl.app.*\"#g" $R/$t/$A.json > $T/$t.json
done
sed -e "s#\"$A\"#\"org.webosatl.app.*\"#g" $M/client-permissions.d/$A.app.json > $T/perm.json
echo "{\"id\":\"$N\",\"version\":\"1.0.0\",\"roleFilesPrv\":[\"$R/prv/$N.json\"],\"roleFilesPub\":[\"$R/pub/$N.json\"],\"clientPermissionFiles\":[\"$M/client-permissions.d/$N.app.json\"]}" > $T/manifest.json
python3 - "$T" "$X" <<'PY' || { echo "generated role files failed the check; nothing installed"; exit 1; }
import json, sys
d, exe = sys.argv[1], sys.argv[2]
for n in ("prv", "pub", "perm", "manifest"):
    json.load(open("%s/%s.json" % (d, n)))          # valid JSON
for n in ("prv", "pub"):
    if exe not in open("%s/%s.json" % (d, n)).read():  # the copy names our helper
        sys.exit(1)
PY
cp $T/prv.json $R/prv/$N.json && cp $T/pub.json $R/pub/$N.json && cp $T/perm.json $M/client-permissions.d/$N.app.json \
	&& cp $T/manifest.json $M/manifests.d/$N.json || { echo "copy failed"; sh "$0" remove; exit 1; }
ls-control scan-services >/dev/null 2>&1
grep -o '"exeName":"[^"]*"' $R/prv/$N.json
