#!/bin/sh
# reapply.sh <patch.py> <commit message>: replace the top tvsrc commit with a fresh run of the patch script, export, rebuild
set -e
cd ~/atl2/tvsrc
git reset -q --hard HEAD~1
python3 ~/atl2/tvpatches/$1 .
git commit -qam "$2"
rm -f ~/atl2/patches/android_translation_layer/*.patch
git format-patch -q -o ~/atl2/patches/android_translation_layer 07c34abad5d29864998ed7c7cfc6c80d0765b9df
sh ~/atl2/build/restart.sh finish
