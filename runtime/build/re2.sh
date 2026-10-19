set -e
cd ~/atl2/tvsrc
git reset -q --hard HEAD~2
python3 ~/atl2/tvpatches/touch-debug.py .
git commit -qam "tv: ATL_TOUCH_DEBUG=1 logs pointer dispatch (picked chain, view subtree, Java classes, phase, result) and click gestures"
python3 ~/atl2/tvpatches/touch-redirect.py .
git commit -qam "tv: input an overlay declines goes to the views underneath: touch sequences (clickable views take taps -> performClick; others get MotionEvents; GTK buttons clicked) and wheel scrolls; view backgrounds are never click targets"
rm -f ~/atl2/patches/android_translation_layer/*.patch
git format-patch -q -o ~/atl2/patches/android_translation_layer 07c34abad5d29864998ed7c7cfc6c80d0765b9df
sh ~/atl2/build/restart.sh finish
